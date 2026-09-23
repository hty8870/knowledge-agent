# -*- coding: utf-8 -*-
"""provision / loadsmoke 回写：把实测报告落成活台账新的一帧（integrity 维 + load 维落盘）。

管道形态（刻意沿用 patrol 的两段式，不造第二套）：

  download_executor --report-json report.json   →   本脚本   →   snapshots/provision-*.jsonl
  （实下实算 md5，产 ProvisionReport）             （本文件）     + 重建 current.json
  load_smoke --out report.json                  →   本脚本   →   snapshots/loadsmoke-*.jsonl
  （抽样真下载真加载，产 loadsmoke 报告）                         + 重建 current.json

与 seed / patrol 共用同一组函数：`patrol_links.load_manifest`（文件宇宙）、
`patrol_links.rebuild_current`（overlay + canon + 物化视图）、`patrol_links.write_snapshot_frame`
/ `write_current`（唯一写盘口）。integrity 走 rebuild_current 的 additive `"i"` 槽
（verified|mismatch；没实测的文件不留这个键，读取侧 inspection.py 派生成 unknown）；
load 走 additive `"l"` 槽（loaded|failed；skipped_unsupported/download_failed 不留键）。

诚实口径（与 patrol 一致）：
  * 下成了 → reachable=ok（服务器真给了字节）；HTTP 4xx/5xx → dead（确定答案）；
    网络错/超时 → unknown（不结论，不覆盖旧结论之外的维度）。
  * size 由「实际下载字节数 vs 清单记录 bytes」重判（不信报告自报，与 seed 不盲信 size_match 同理）。
  * skipped(flagged) / rejected 没有产生任何新证据 → 不写进帧。
  * problem/reason 仍不落盘，由 inspection.py 读取时派生。

只写 `data/inspection/`（或 --out-dir 指定处，供测试），绝不碰 `database/base/`、绝不改清单。

用法：
  py scripts/record_provision_results.py --report 路径/report.json [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import patrol_links as P  # noqa: E402

REPORT_SCHEMA = "biodata-provision/v0"
LOAD_REPORT_SCHEMA = "biodata-loadsmoke/v0"   # load_smoke 抽样冒烟报告（含下载证据 + load 判定）

#: 执行器状态 → 台账可达性证据。skipped/rejected 没有新证据，不进表（= 不写帧）。
_REACH = {
    "ok": "ok", "size_ok": "ok", "md5_mismatch": "ok", "size_mismatch": "ok", "unverified": "ok",
}
_INTEGRITY = {"ok": "verified", "md5_mismatch": "mismatch"}
#: load_smoke 判定 → 台账 load 档（loaded|failed；skipped_unsupported/download_failed 不留键 = 未实测）。
_LOAD = {"loaded": "loaded", "load_failed": "failed"}


def load_report(path: Path) -> dict:
    """读报告并 fail-closed 校验形状：只认已知的两种 schema，不是执行器/冒烟产出的东西宁可拒收。"""
    try:
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"报告读不出来：{e}")
    if not isinstance(rep, dict) or rep.get("schema") not in (REPORT_SCHEMA, LOAD_REPORT_SCHEMA):
        raise ValueError(
            f"报告 schema 不是 {REPORT_SCHEMA} / {LOAD_REPORT_SCHEMA}："
            "只收 download_executor / load_smoke 的结构化输出，不猜别的形状。")
    if not isinstance(rep.get("results"), list):
        raise ValueError("报告缺 results 列表。")
    return rep


def _row_from_download(status, http_status, got, rec_bytes) -> dict:
    """下载实测 → 台账行 {reach,http,srv,size[,integrity]}（provision / loadsmoke 共用这一段口径）。"""
    if status == "unreachable":
        reach = "dead" if http_status else "unknown"   # HTTP 错误是确定答案；网络错不结论
        srv = None
    else:
        reach = _REACH.get(status, "unknown")
        srv = got
    row = {
        "reach": reach,
        "http": http_status,
        "srv": srv,
        "size": P._size_state(srv, rec_bytes),
    }
    integ = _INTEGRITY.get(status)
    if integ:
        row["integrity"] = integ
    return row


def results_from_report(rep: dict, rec_bytes_by_key: dict) -> "tuple[dict, int]":
    """ProvisionReport → rebuild_current 的 results（key(uid\\turl) → {reach,http,srv,size[,integrity]}）。

    返回 (results, n_no_evidence)：skipped(flagged)/rejected 不计入帧，只计数披露。
    """
    results: dict[str, dict] = {}
    n_no_evidence = 0
    for r in rep["results"]:
        if not isinstance(r, dict):
            continue
        status = r.get("status")
        uid, url = r.get("dataset_uid"), r.get("url")
        if not uid or not url:
            continue
        if status in ("skipped_flagged", "rejected"):
            n_no_evidence += 1
            continue
        key = f"{uid}\t{P.norm(url)}"
        if key not in rec_bytes_by_key:
            # 执行器只下清单里的文件；对不上说明报告被改过或清单已变——拒收这一行，不硬 join。
            print(f"[record] 警告：{uid} 的 {url[:80]} 不在本机文件清单里，跳过。", file=sys.stderr)
            continue
        results[key] = _row_from_download(status, r.get("http_status"),
                                          r.get("bytes_downloaded"), rec_bytes_by_key[key])
    return results, n_no_evidence


def results_from_load_report(rep: dict, rec_bytes_by_key: dict) -> "tuple[dict, int]":
    """load_smoke 报告 → rebuild_current 的 results，比 provision 多一个可选 `load` 槽。

    下载证据（reach/http/srv/size/integrity）与 provision 同口径——冒烟本来就是先真下载；
    load 判定只有 loaded / load_failed 才落 `"l"`，skipped_unsupported / download_failed
    没有产生加载结论 → 不留键（= 未实测），诚实降级不硬解。
    """
    results: dict[str, dict] = {}
    n_no_evidence = 0
    for r in rep["results"]:
        if not isinstance(r, dict):
            continue
        status = r.get("download_status")
        uid, url = r.get("dataset_uid"), r.get("url")
        if not uid or not url:
            continue
        if status in ("skipped_flagged", "rejected"):
            n_no_evidence += 1
            continue
        key = f"{uid}\t{P.norm(url)}"
        if key not in rec_bytes_by_key:
            print(f"[record] 警告：{uid} 的 {url[:80]} 不在本机文件清单里，跳过。", file=sys.stderr)
            continue
        row = _row_from_download(status, r.get("http_status"),
                                 r.get("bytes_downloaded"), rec_bytes_by_key[key])
        load = _LOAD.get(r.get("load_status"))
        if load:
            row["load"] = load
        results[key] = row
    return results, n_no_evidence


def record(report_path, *, out_dir=None, dry_run: bool = False,
           snapshot_iso: "str | None" = None) -> int:
    out = Path(out_dir) if out_dir else P._OUT_DIR
    snap_dir = out / "snapshots"
    current_path = out / "current.json"

    try:
        rep = load_report(Path(report_path))
    except ValueError as e:
        print(f"[record] {e}", file=sys.stderr)
        return 2
    if not P._MANIFEST.exists():
        print(f"[record] manifest 缺失：{P._MANIFEST}", file=sys.stderr)
        return 2

    manifest = P.load_manifest()
    rec_bytes_by_key = {f"{u}\t{url}": b for u in manifest for (url, b) in manifest[u]}
    is_loadsmoke = rep.get("schema") == LOAD_REPORT_SCHEMA
    source = "loadsmoke" if is_loadsmoke else "provision"
    if is_loadsmoke:
        results, n_no_evidence = results_from_load_report(rep, rec_bytes_by_key)
    else:
        results, n_no_evidence = results_from_report(rep, rec_bytes_by_key)
    if not results:
        print("[record] 报告里没有任何可回写的实测证据（全部 skipped/rejected/对不上清单）：不落帧。")
        return 0

    prior = {}
    if current_path.exists():
        try:
            prior = json.loads(current_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior = {}

    iso = snapshot_iso or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current, snap_rows, snap_id, tally = P.rebuild_current(manifest, prior, results, iso, source=source)

    integ_note = ""
    if tally.get("integrity_verified") or tally.get("integrity_mismatch"):
        integ_note = (f" | integrity verified/mismatch="
                      f"{tally.get('integrity_verified', 0)}/{tally.get('integrity_mismatch', 0)}")
    load_note = ""
    if tally.get("load_loaded") or tally.get("load_failed"):
        load_note = f" | load loaded/failed={tally.get('load_loaded', 0)}/{tally.get('load_failed', 0)}"
    print(f"[record] snapshot_id={snap_id} date={iso} source={source}")
    print(f"[record] 回写 {len(results)} 条实测（无证据未入帧 {n_no_evidence} 条）"
          f" | problem {tally['problem']}{integ_note}{load_note}")

    if dry_run:
        print("[record] --dry-run：不落盘。")
        return 0

    snap_path = snap_dir / f"{source}-{iso[:10]}-{snap_id}.jsonl"
    P.write_snapshot_frame(snap_path, {"schema": P.SCHEMA, "snapshot_id": snap_id, "source": source,
                                       "snapshot_date": iso, "n_files": tally["files"],
                                       "n_problem": tally["problem"], "tally": tally,
                                       "probed": len(results),
                                       "provision_started_at": rep.get("started_at", "")}, snap_rows)
    P.write_current(current_path, current)
    print(f"[record] -> {snap_path}\n[record] -> {current_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="provision/loadsmoke 回写：实测报告 → 台账新帧（含 integrity/load 维）+ 重建 current.json")
    ap.add_argument("--report", required=True,
                    help="download_executor --report-json 或 load_smoke --out 产出的报告（按 schema 自动识别）")
    ap.add_argument("--out-dir", default="", help="覆盖台账目录（默认 data/inspection；测试用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不落盘")
    args = ap.parse_args()
    return record(args.report, out_dir=args.out_dir or None, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
