# -*- coding: utf-8 -*-
"""巡检重测：给活台账再敲一帧——把「一次性快照」变成「随时间刷新的活台账」。

对文件级直链重做轻量存活实测（Range-GET bytes=0-0），产出**新的一帧内容寻址快照**并重建
`data/inspection/current.json`；两帧一 diff（`diff_snapshots.py`）即「自上次以来谁死了/谁恢复了/
谁大小变了」。用 stdlib urllib（不引入 requests，保持主线依赖精简）。

安全边界（务必读）：
  * **默认 --report-only：不发任何网络请求**，只把已有 current.json 重新聚合/落一帧。真正联网要显式 --probe。
  * 只碰 10x 两个公开 host（cf.10xgenomics.com / s3-us-west-2.amazonaws.com/10x.files），每请求只读 1 字节。
  * `--sample-pct` / `--limit` / `--uids` 做**滚动抽样**，避免一次性对上游 15,119 次全量敲击（既省事又不挑衅上游）。
  * 只写 `data/inspection/`，绝不碰 `database/base/`、绝不改 manifest、绝不入 .env。

诚实分桶（与阶段二同口径）：alive(200/206) / http_dead(4xx/5xx) / network_error(超时·连接错=不结论) / off_host。
size：服务器报告总大小 vs manifest 记录 bytes → match|mismatch|unknown。integrity(md5)/load 本步不做，仍 unknown。

用法：
  py scripts/patrol_links.py                          # 默认 report-only：不联网，按现有 current 重落一帧
  py scripts/patrol_links.py --probe --limit 20       # 真实测前 20 个文件（冒烟/试跑）
  py scripts/patrol_links.py --probe --sample-pct 5   # 滚动抽样 5% 重测
  py scripts/patrol_links.py --probe --uids uidA,uidB # 只重测指定数据集
  py scripts/patrol_links.py --probe --workers 8      # 全量重测（对上游 15k 次，请谨慎/限速）
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import hashlib
import json
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

_AGENT = Path(__file__).resolve().parent.parent
_MANIFEST = _AGENT / "src" / "dataset_recommender" / "data" / "download_links.by_uid.json"
_OUT_DIR = _AGENT / "src" / "dataset_recommender" / "data" / "inspection"
_SNAP_DIR = _OUT_DIR / "snapshots"
_CURRENT = _OUT_DIR / "current.json"

SCHEMA = "biodata-inspection/v0"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ALLOWED_HOST_RE = re.compile(
    r"^https://(?:cf\.10xgenomics\.com|s3-us-west-2\.amazonaws\.com/10x\.files)/")


def norm(u: str) -> str:
    if not u or "://" not in u:
        return u or ""
    scheme, rest = u.split("://", 1)
    for sep in ("?", "#"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    rest = re.sub(r"/{2,}", "/", rest)
    rest = rest.replace("\\u002F", "/").replace("\\/", "/")
    return f"{scheme}://{rest}"


def _size_state(server_total, rec_bytes) -> str:
    if server_total is None or rec_bytes is None:
        return "unknown"
    try:
        return "match" if int(server_total) == int(rec_bytes) else "mismatch"
    except (TypeError, ValueError):
        return "unknown"


def _parse_total(headers, status: int):
    cr = headers.get("Content-Range")           # e.g. "bytes 0-0/12345"
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            return int(tail)
    if status == 200:
        cl = headers.get("Content-Length")
        if cl and str(cl).isdigit():
            return int(cl)
    return None


def probe(url: str) -> dict:
    """轻量存活探测：Range-GET 1 字节。返回 {bucket,status,server_total}。只读、越界即跳过。"""
    if not ALLOWED_HOST_RE.match(url):
        return {"bucket": "off_host", "status": None, "server_total": None}
    req = urllib.request.Request(url, method="GET",
                                 headers={"Range": "bytes=0-0", "User-Agent": UA})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                status = getattr(r, "status", None) or r.getcode()
                total = _parse_total(r.headers, status)
                r.read(1)
            ok = status in (200, 206)
            return {"bucket": "alive" if ok else "http_dead",
                    "status": status, "server_total": total}
        except urllib.error.HTTPError as e:
            return {"bucket": "http_dead", "status": e.code, "server_total": None}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = repr(e)
    return {"bucket": "network_error", "status": None, "server_total": None, "error": last}


def load_manifest() -> "dict[str, list[tuple[str, int | None]]]":
    """uid -> [(norm_url, rec_bytes)]，文件宇宙。"""
    by = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    out: dict[str, list] = {}
    for uid, rec in by.items():
        if not isinstance(rec, dict):
            continue
        fs = []
        for f in rec.get("files", []) or []:
            if isinstance(f, dict) and f.get("download_url"):
                fs.append((norm(f["download_url"]), f.get("bytes")))
        if fs:
            out[uid] = fs
    return out


def select_targets(manifest, args) -> "list[tuple[str, str, int | None]]":
    """按 --uids / --limit / --sample-pct 选出要重测的 (uid, url, rec_bytes)。"""
    uids = manifest.keys()
    if args.uids:
        want = {u.strip() for u in args.uids.split(",") if u.strip()}
        uids = [u for u in manifest if u in want]
    flat = [(u, url, b) for u in uids for (url, b) in manifest[u]]
    if args.sample_pct:
        k = max(1, round(len(flat) * args.sample_pct / 100.0))
        rng = random.Random(args.seed)
        flat = rng.sample(flat, min(k, len(flat)))
    if args.limit:
        flat = flat[:args.limit]
    return flat


def rebuild_current(manifest, prior: dict, results: dict, snapshot_iso: str, source: str) -> dict:
    """把本次实测结果 overlay 到既有 current 上（未测文件保留旧向量），重算 per-uid/totals。

    results: key(uid\\turl) -> {reach,http,srv,size}。prior: 既有 current.json（首次可空）。
    additive 扩展（provision 回写）：results 可带可选 `integrity`
    （verified|mismatch|unknown，由 download_executor 实下实算 md5 得来）。带则写进向量的
    `"i"` 槽并计入 problem；**没带则保留该文件旧向量的 `"i"`**——巡逻重测的是存活/大小，
    不该顺手抹掉已实测的 integrity 结论。完全无 integrity 的帧，canon 与旧格式逐位一致。
    additive 扩展（load_smoke 回写）：results 可带可选 `load`
    （loaded|failed，由 load_smoke 抽样真下载真加载得来）。带则写进向量的 `"l"` 槽，
    failed 计入 problem；没带则保留旧值，与 `"i"` 同理。不带 `"l"` 的帧 canon 逐位不变
    （`i`/`l` 词表不相交，canon 追加段不会撞 id）。
    """
    day = snapshot_iso[:10]
    prior_uid = (prior or {}).get("by_uid", {})
    by_uid: dict[str, dict] = {}
    tally = {"files": 0, "reach_ok": 0, "reach_dead": 0, "reach_unknown": 0,
             "size_match": 0, "size_mismatch": 0, "size_unknown": 0, "problem": 0}

    for uid, files in manifest.items():
        prior_files = (prior_uid.get(uid) or {}).get("f", {})
        u = {"lv": (prior_uid.get(uid) or {}).get("lv", "unknown"), "nf": 0, "np": 0, "f": {}}
        touched = False
        for url, rec_bytes in files:
            key = f"{uid}\t{url}"
            if key in results:
                res = results[key]
                vec = {"r": res["reach"], "h": res["http"], "s": res["size"],
                       "srv": res["srv"], "v": day}
                integ = res.get("integrity")
                if integ is None:
                    # 本次重测没带 integrity 结论（如 patrol 只探存活）→ 保留旧值，不顺手抹掉。
                    integ = (prior_files.get(url) or {}).get("i")
                if integ:
                    vec["i"] = integ
                load = res.get("load")
                if load is None:
                    # 本次重测没带 load 结论 → 保留旧值，与 integrity 同理。
                    load = (prior_files.get(url) or {}).get("l")
                if load:
                    vec["l"] = load
                touched = True
            elif url in prior_files:
                vec = dict(prior_files[url])            # 未测：保留旧向量（含其旧 last_verified 与 i）
            else:
                vec = {"r": "unknown", "h": None, "s": "unknown", "srv": None, "v": "unknown"}
            problem = ((vec["r"] == "dead") or (vec["s"] == "mismatch")
                       or (vec.get("i") == "mismatch") or (vec.get("l") == "failed"))
            u["f"][url] = vec
            u["nf"] += 1
            if problem:
                u["np"] += 1
            tally["files"] += 1
            tally[f'reach_{vec["r"]}'] = tally.get(f'reach_{vec["r"]}', 0) + 1
            tally[f'size_{vec["s"]}'] = tally.get(f'size_{vec["s"]}', 0) + 1
            if vec.get("i"):
                tally[f'integrity_{vec["i"]}'] = tally.get(f'integrity_{vec["i"]}', 0) + 1
            if vec.get("l"):
                tally[f'load_{vec["l"]}'] = tally.get(f'load_{vec["l"]}', 0) + 1
            if problem:
                tally["problem"] += 1
        if touched:
            u["lv"] = day                                # 本数据集有文件被重测 → 刷新数据集级核验日
        by_uid[uid] = u

    snap_rows = sorted(
        ({**{"k": f"{uid}\t{url}", "r": v["r"], "h": v["h"], "s": v["s"], "srv": v["srv"]},
          **({"i": v["i"]} if v.get("i") else {}),
          **({"l": v["l"]} if v.get("l") else {})}
         for uid, u in by_uid.items() for url, v in u["f"].items()),
        key=lambda r: r["k"])
    # canon：无 integrity/load 的帧与旧格式逐位一致；带 i/l 的行顺次追加段，避免与纯存活帧撞 id
    # （i 词表 verified|mismatch 与 l 词表 loaded|failed 不相交，追加位置不产生歧义）。
    canon = "\n".join(f'{r["k"]}|{r["r"]}|{r["h"]}|{r["s"]}|{r["srv"]}'
                      + (f'|{r["i"]}' if r.get("i") else "")
                      + (f'|{r["l"]}' if r.get("l") else "") for r in snap_rows)
    snap_id = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    current = {"schema": SCHEMA, "snapshot_id": snap_id, "snapshot_date": snapshot_iso,
               "source": source, "totals": tally, "by_uid": by_uid}
    return current, snap_rows, snap_id, tally


def run(args) -> int:
    if not _MANIFEST.exists():
        print(f"[patrol] manifest 缺失：{_MANIFEST}", file=sys.stderr)
        return 2
    manifest = load_manifest()
    prior = {}
    if _CURRENT.exists():
        try:
            prior = json.loads(_CURRENT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior = {}

    results: dict[str, dict] = {}
    if args.probe:
        targets = select_targets(manifest, args)
        rec_by_key = {f"{u}\t{url}": b for u in manifest for (url, b) in manifest[u]}
        print(f"[patrol] 实测目标 {len(targets)} 个文件 | workers={args.workers}", flush=True)

        def work(t):
            uid, url, rec_bytes = t
            res = probe(url)
            return f"{uid}\t{url}", {
                "reach": {"alive": "ok", "http_dead": "dead"}.get(res["bucket"], "unknown"),
                "http": res.get("status"),
                "srv": res.get("server_total"),
                "size": _size_state(res.get("server_total"), rec_bytes),
                "bucket": res["bucket"],
            }

        n = 0
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for key, vec in ex.map(work, targets):
                results[key] = vec
                n += 1
                if n % 200 == 0 or vec["bucket"] in ("http_dead", "off_host"):
                    print(f"  [{n}/{len(targets)}] {vec['bucket']:12s} {vec['http']} {key.split(chr(9))[0][:34]}", flush=True)
        print(f"[patrol] 实测完成 {n} 条。", flush=True)
    else:
        print("[patrol] --report-only（默认）：不联网，仅按现有 current 重新落一帧。加 --probe 才真实测。")

    snapshot_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current, snap_rows, snap_id, tally = rebuild_current(
        manifest, prior, results, snapshot_iso, source="patrol" if args.probe else "report")

    print(f"[patrol] snapshot_id={snap_id} date={snapshot_iso}")
    print(f"[patrol] 文件 {tally['files']} | reachable ok/dead/unknown="
          f"{tally['reach_ok']}/{tally['reach_dead']}/{tally['reach_unknown']} "
          f"| size match/mismatch/unknown={tally['size_match']}/{tally['size_mismatch']}/{tally['size_unknown']}"
          f" | problem {tally['problem']}")

    if args.dry_run:
        print("[patrol] --dry-run：不落盘。")
        return 0

    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    tag = "patrol" if args.probe else "report"
    snap_path = _SNAP_DIR / f"{tag}-{snapshot_iso[:10]}-{snap_id}.jsonl"
    write_snapshot_frame(snap_path, {"schema": SCHEMA, "snapshot_id": snap_id,
                                     "source": current["source"], "snapshot_date": snapshot_iso,
                                     "n_files": tally["files"], "n_problem": tally["problem"],
                                     "tally": tally, "probed": len(results)}, snap_rows)
    write_current(_CURRENT, current)
    print(f"[patrol] -> {snap_path.relative_to(_AGENT)}\n[patrol] -> {_CURRENT.relative_to(_AGENT)}")
    return 0


def write_snapshot_frame(snap_path, meta: dict, snap_rows: list):
    """落一帧内容寻址快照（首行 `_meta`，其后每文件一行紧凑向量）。

    patrol / record_provision_results 共用这一个写盘口——帧格式只有一份定义，不许各写各的。
    """
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    with snap_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for r in snap_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return snap_path


def write_current(path, current: dict):
    """落物化视图 current.json。与 write_snapshot_frame 同理，单一口子。"""
    path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="活台账巡检重测：新增一帧内容寻址快照 + 重建 current.json")
    ap.add_argument("--probe", action="store_true", help="真实发起网络实测（默认关＝report-only 不联网）")
    ap.add_argument("--workers", type=int, default=8, help="并发数（默认 8，别把上游敲太狠）")
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 个文件（试跑/冒烟）")
    ap.add_argument("--sample-pct", type=float, default=0.0, help="滚动抽样百分比（如 5 = 5%%）")
    ap.add_argument("--uids", type=str, default="", help="只测这些 dataset_uid（逗号分隔）")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子（可复现）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不落盘")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
