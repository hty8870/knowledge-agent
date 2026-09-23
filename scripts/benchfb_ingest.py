#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""benchmark 采集反馈包 · 接收侧 ingest。

用户在「设置 → 使用反馈 → 导出反馈包」得到单个 JSON 文件（schema
`biodata-benchfb/1`），微信发回。本脚本把一个或多个包变成我们能直接用的三样东西：

1. `merged.json`       —— 全部记录合并去重后的总包（真源，供后续一切加工）。
2. `review.html`       —— 人读审阅报告：每条查询的路由/结果/轨迹/评分一页看尽，
                          用户标出的「有用条目」高亮——逐条人工核对的金标来源。
3. `candidates.jsonl` —— benchmark 候选：每行一条 {query, 系统 top-k, 用户标注的相关
                          条目, 完成度/原因（旧包为星级）, 评语, 路由/耗时/环境}，可直接进 eval 构造流程。

用法：
    python scripts/benchfb_ingest.py 包1.json 包目录/ [--out 输出目录]

设计纪律（与项目同）：只读不写输入文件；坏包不炸批（逐文件记错继续）；去重键
(install_id, record.id)，先到先留；纯标准库。
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "biodata-benchfb/1"


def _load_packages(paths: list[Path]) -> tuple[list[dict], list[str]]:
    """读入全部包；返回 (records 带 _install/_src, 错误清单)。坏包记错不炸批。"""
    records: list[dict] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        try:
            pkg = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 —— 坏 JSON/编码问题都记入错误清单
            errors.append(f"{path.name}: 读不了（{exc}）")
            continue
        if not isinstance(pkg, dict) or pkg.get("schema") != SCHEMA:
            errors.append(f"{path.name}: schema 不是 {SCHEMA}（是 {pkg.get('schema')!r}）——跳过")
            continue
        install = str(pkg.get("install_id") or "")
        recs = pkg.get("records")
        if not isinstance(recs, list):
            errors.append(f"{path.name}: records 不是数组——跳过")
            continue
        for rec in recs:
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            key = (install, str(rec["id"]))
            if key in seen:
                continue   # 同一台机器的同一个包被发来两次：静默去重，不记错误
            seen.add(key)
            rec = dict(rec)
            rec["_install"] = install
            rec["_src"] = path.name
            records.append(rec)
    records.sort(key=lambda r: float(r.get("t") or 0))
    return records, errors


def _topk_uids(rec: dict) -> list[str]:
    res = ((rec.get("search") or {}).get("res") or {}).get("results") or []
    return [str(it.get("dataset_uid") or "") for it in res if isinstance(it, dict)]


def _candidate(rec: dict) -> dict[str, Any]:
    """一条记录 → 一行 benchmark 候选（eval 构造的直接输入）。

    rating 容忍新旧两种形状（评分结构改版后）：新形状
    {completion, reasons, useful_idx, useful_uids, comment, rated_at} 不再写 stars；
    旧形状 {stars, useful_idx, comment, rated_at}。两种都原样带出，缺侧为 None/空，
    不报错、不编造。"""
    rating = rec.get("rating") or {}
    resolved = rating.get("useful_resolved") or []
    useful_uids = rating.get("useful_uids")
    if not isinstance(useful_uids, list):   # 旧包没有 useful_uids：从 useful_resolved 解析兜底
        useful_uids = [str(x.get("uid") or "") for x in resolved if isinstance(x, dict)]
    route = rec.get("route") or {}
    search = rec.get("search") or {}
    res = search.get("res") or {}
    env = rec.get("env") or {}
    return {
        "record_id": rec.get("id"),
        "install_id": rec.get("_install", ""),
        "ts": rec.get("t"),
        "tid": rec.get("tid") or "",
        "kind": rec.get("kind"),
        "query": rec.get("q"),
        "effective_query": route.get("query") or (search.get("req") or {}).get("query") or "",
        "route": route.get("route") or "",
        "via": route.get("via") or "",
        "resolution_status": res.get("resolution_status") or "",
        "result_total": res.get("result_total"),
        "system_topk_uids": _topk_uids(rec),
        "rating": {
            "stars": rating.get("stars"),
            "completion": rating.get("completion"),
            "reasons": [str(x) for x in (rating.get("reasons") or []) if isinstance(x, str)],
            "useful_uids": [str(u) for u in useful_uids],
            "useful_idx": rating.get("useful_idx") or [],
            "comment": rating.get("comment") or "",
        },
        "action_verb": (rec.get("action") or {}).get("verb") or "",
        "action_cancelled": bool((rec.get("action") or {}).get("cancelled")),
        "timing_ms": {"route": rec.get("route_ms") or 0, "search": search.get("ms") or 0},
        "cached": bool(search.get("cached")),
        "env": {"model": env.get("model") or "", "provider": env.get("provider") or "", "endpoint_host": env.get("endpoint_host") or ""},
        "error": rec.get("err") or "",
        # 模板轮次标记随候选携带；写 candidates 前默认已排除
        # template_originated=true 的轮次（与 telemetry_export.py 同一口径）。
        "template_originated": rec.get("template_originated"),
    }


_KIND_ZH = {"search": "检索", "tool": "操作", "none": "回音", "error": "失败", "unknown": "未完成"}

# 完成度三选（新评分形状）的人读标签；旧形状记录的星级在 _rate_txt 里兜底。
_COMP_ZH = {"done": "完成", "partial": "部分完成", "failed": "未完成"}


def _rate_txt(rating: dict) -> str:
    comp = str(rating.get("completion") or "")
    if comp in _COMP_ZH:
        return _COMP_ZH[comp]
    stars = rating.get("stars")
    if stars:
        return "★" * int(stars) + "☆" * (5 - int(stars))
    return "未评分"


def _is_rated(rec: dict) -> bool:
    rating = rec.get("rating") or {}
    return bool(rating.get("completion") or rating.get("stars")
                or rating.get("reasons") or rating.get("useful_idx") or rating.get("comment"))


def _trace_lines(rec: dict) -> list[str]:
    """把执行轨迹压成人读行：检索的 search_trace 各步 + agent 的 plan.trace 各节点。"""
    lines: list[str] = []
    steps = (((rec.get("search") or {}).get("res") or {}).get("search_trace") or {}).get("steps") or []
    for st in steps:
        if isinstance(st, dict):
            ms = st.get("duration_ms")
            lines.append(f"检索·{st.get('id', '?')}：{st.get('status', '?')}"
                         + (f"（{round(float(ms))}ms）" if ms else "")
                         + (f"——{st.get('reason')}" if st.get("reason") else ""))
    plan = (rec.get("route") or {}).get("plan") or {}
    for st in plan.get("trace") or []:
        if isinstance(st, dict):
            lines.append(f"agent·{st.get('node', '?')}：{'✗ ' + str(st.get('detail')) if st.get('ok') is False else 'ok'}")
    for st in (rec.get("action") or {}).get("trace") or []:
        if isinstance(st, dict):
            lines.append(f"执行·{st.get('text', '?')}" + (f"——{st.get('detail')}" if st.get("detail") else ""))
    return lines


def _review_html(records: list[dict], errors: list[str], sources: list[str]) -> str:
    """人读审阅报告：一条查询一卡，评分/标注高亮，轨迹折叠。全部转义。"""
    e = html.escape
    cards: list[str] = []
    for rec in reversed(records):   # 新的在前
        rating = rec.get("rating") or {}
        resolved = {str(x.get("uid")) for x in (rating.get("useful_resolved") or []) if isinstance(x, dict)}
        rate_txt = _rate_txt(rating)
        reasons = [str(x) for x in (rating.get("reasons") or []) if isinstance(x, str)]
        rows = []
        for i, it in enumerate((((rec.get("search") or {}).get("res") or {}).get("results") or []), 1):
            if not isinstance(it, dict):
                continue
            uid = str(it.get("dataset_uid") or "")
            mark = ' class="hit"' if uid in resolved else ""
            rows.append(
                f"<tr{mark}><td>{i}</td><td>{e(uid)}</td><td>{e(str(it.get('dataset_name') or it.get('title') or ''))}</td>"
                f"<td>{e(str(it.get('source') or ''))}</td></tr>")
        results_html = ("<table><thead><tr><th>#</th><th>编号</th><th>名称</th><th>来源</th></tr></thead><tbody>"
                        + "".join(rows) + "</tbody></table>") if rows else "<p class='dim'>（无检索结果段）</p>"
        trace = _trace_lines(rec)
        trace_html = ("<details><summary>执行过程（" + str(len(trace)) + " 步）</summary><ol>"
                      + "".join(f"<li>{e(t)}</li>" for t in trace) + "</ol></details>") if trace else ""
        comment = str(rating.get("comment") or "")
        env = rec.get("env") or {}
        cards.append(f"""
<section class="card">
  <header>
    <span class="kind k-{e(str(rec.get('kind') or 'unknown'))}">{e(_KIND_ZH.get(str(rec.get('kind')), str(rec.get('kind') or '?')))}</span>
    <span class="q">{e(str(rec.get('q') or ''))}</span>
    <span class="stars">{e(rate_txt)}</span>
  </header>
  <div class="meta">路由 {e(str((rec.get('route') or {}).get('route') or '—'))} · 经 {e(str((rec.get('route') or {}).get('via') or '—'))}
    · 检索 {e(str((rec.get('search') or {}).get('ms') or 0))}ms · 模型 {e(str(env.get('model') or '—'))}
    · {e(str(rec.get('_src') or ''))} · id {e(str(rec.get('id') or ''))}</div>
  {f'<div class="comment">原因：{e("、".join(reasons))}</div>' if reasons else ''}
  {f'<div class="comment">评语：{e(comment)}</div>' if comment else ''}
  {f'<div class="err">错误：{e(str(rec.get("err")))}</div>' if rec.get('err') else ''}
  {results_html}
  {trace_html}
</section>""")
        # f-string 内的反斜杠限制：上面已规避（无反斜杠表达式）。
    rated = sum(1 for r in records if _is_rated(r))
    err_html = "".join(f"<li>{e(x)}</li>" for x in errors)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>benchmark 反馈审阅 · {len(records)} 条</title>
<style>
body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:1080px;margin:24px auto;padding:0 16px;color:#1c1f24}}
h1{{font-size:20px}} .sum{{color:#5b6068;margin-bottom:18px}}
.card{{border:1px solid #dde3ea;border-radius:12px;padding:14px 16px;margin:14px 0;background:#fffdf9}}
.card header{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.kind{{flex:0 0 auto;padding:1px 10px;border-radius:999px;font-size:12px;font-weight:700;color:#fff;background:#8a8f98}}
.k-search{{background:#0d9488}} .k-tool{{background:#7c5cd6}} .k-none{{background:#b78900}} .k-error{{background:#c0392b}}
.q{{font-weight:700;font-size:15px}} .stars{{margin-left:auto;color:#e8a33d;font-size:16px}}
.meta{{color:#8a8f98;font-size:12px;margin:4px 0 8px}}
.comment{{background:#fff7e6;border-left:3px solid #e8a33d;padding:6px 10px;margin:8px 0;font-size:13px}}
.err{{background:#fdecea;border-left:3px solid #c0392b;padding:6px 10px;margin:8px 0;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}}
td,th{{border:1px solid #e6ebf0;padding:4px 8px;text-align:left}}
tr.hit td{{background:#e6f5ee;font-weight:600}}
tr.hit td:first-child::after{{content:" ✓有用";color:#0a875a}}
details{{margin-top:8px;font-size:13px}} summary{{cursor:pointer;color:#5b6068}}
.dim{{color:#8a8f98}} .errs{{background:#fdecea;padding:10px 14px;border-radius:10px}}
</style></head><body>
<h1>benchmark 反馈审阅</h1>
<p class="sum">共 {len(records)} 条记录（{len(set(r.get('_install') for r in records))} 台机器 · 来源：{e('、'.join(sources))}）；
已评分 {rated} 条；高亮行 = 用户标出的「有用条目」。生成于 {e(datetime.now(timezone.utc).isoformat(timespec='seconds'))}</p>
{f'<div class="errs"><strong>坏包/跳过：</strong><ul>{err_html}</ul></div>' if errors else ''}
{''.join(cards)}
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="benchmark 采集反馈包 ingest：校验 → 去重合并 → 审阅报告 + 候选 JSONL")
    ap.add_argument("inputs", nargs="+", help="反馈包 JSON 文件，或装着反馈包的目录（取其中全部 .json）")
    ap.add_argument("--out", default="benchfb_out", help="输出目录（默认 ./benchfb_out）")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for raw in args.inputs:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.json")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[warn] 找不到：{p}", file=sys.stderr)
    if not files:
        print("没有可读的反馈包文件。", file=sys.stderr)
        return 2

    records, errors = _load_packages(files)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    merged = {
        "schema": SCHEMA,
        "merged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [p.name for p in files],
        "installs": sorted({r.get("_install", "") for r in records}),
        "records": records,
    }
    (out_dir / "merged.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    # benchmark 候选默认排除「未经编辑直接发送」的模板轮次
    # （template_originated=true——模板句不是用户自己组织的检索词；与 telemetry_export.py
    # 的候选生成同口径）。merged.json 仍全量保留，事后审计可查。
    candidates = [r for r in records if r.get("template_originated") is not True]
    (out_dir / "candidates.jsonl").write_text(
        "".join(json.dumps(_candidate(r), ensure_ascii=False) + "\n" for r in candidates), encoding="utf-8")
    (out_dir / "review.html").write_text(_review_html(records, errors, [p.name for p in files]), encoding="utf-8")

    rated = sum(1 for r in records if _is_rated(r))
    marked = sum(len((r.get("rating") or {}).get("useful_resolved") or []) for r in records)
    print(f"读入 {len(files)} 个包 → {len(records)} 条记录（去重后）；已评分 {rated} 条；用户标注有用 {marked} 条。")
    for err in errors:
        print(f"[跳过] {err}", file=sys.stderr)
    print(f"产物：{out_dir}/merged.json · review.html · candidates.jsonl")
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
