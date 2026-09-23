# -*- coding: utf-8 -*-
"""加载抽样冒烟：抽样真下载 primary 主文件并用 scanpy 真加载，把台账 load 维从「恒 unknown」变成实测。

provisioning 主线的最后一格：patrol 证明「链接活着」、provision 证明「下下来 md5 对得上」，
本脚本证明「这份矩阵真能装进 Scanpy」。刻意不重造轮子：

- 抽样自 `data/download_links.by_uid.json` 清单：只选 primary 主文件为 `.h5`（10x filtered
  feature-barcode matrix 一族）的数据集，按 `platform` 字段分层、层内均匀随机抽样，
  `--seed` 固定则结果逐位可复现。
- 下载复用 `download_executor.provision`（同一份白名单/旗标/原子落盘口径）；
  本脚本自己一个网络字节都不收。
- 加载按文件类型分发：`.h5` → `scanpy.read_10x_h5`；其它类型记 `skipped_unsupported`
  并注明原因——诚实降级，不硬解不猜格式。
- scanpy 是**可选依赖**：脚本启动先 `require_scanpy()`，缺失则打印安装指引并非零退出
  （fail-closed）；核心函数 `run_smoke` 接受注入的 readers，测试全程不 import scanpy、不联网。

结果分级（与执行器 verdict 同风格，互不发明）：
  loaded / load_failed(原因) / skipped_unsupported(原因) / download_failed

落台账走下载侧同一回写管道，两段式：
  py scripts/load_smoke.py --dest 绝对路径 --out report.json
  py scripts/record_provision_results.py --report report.json     # load 维落 additive "l" 槽

只写用户显式指定的 --dest（download_executor 的 fail-closed 校验兜底），绝不碰
`database/base/` 与在仓数据目录。真实联网冒烟需用户确认后执行；测试全程禁网、不真装 scanpy。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dataset_recommender.corpus import download_executor as DE  # noqa: E402
from dataset_recommender.corpus import download_plan as DP  # noqa: E402

SCHEMA = "biodata-loadsmoke/v0"

# ---- load 判定词表 ----
LOADED = "loaded"                            # 真加载成功，记下了 shape
LOAD_FAILED = "load_failed"                  # 下载核对通过但 scanpy 读入报错
SKIPPED_UNSUPPORTED = "skipped_unsupported"  # 文件类型暂不支持真加载（诚实降级）
DOWNLOAD_FAILED = "download_failed"          # 没下成 / 没下（unreachable / skipped_flagged / rejected）
LOAD_STATUSES = (LOADED, LOAD_FAILED, SKIPPED_UNSUPPORTED, DOWNLOAD_FAILED)

DEFAULT_SAMPLE = 60          # 默认抽样数据集数
DEFAULT_SEED = 20260801      # 默认固定种子：不显式传 --seed 也可复现

_AGENT_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = Path(os.environ.get(
    "BIODATA_DOWNLOAD_LINKS",
    str(_AGENT_ROOT / "src" / "dataset_recommender" / "data" / "download_links.by_uid.json")))


# ---------------------------------------------------------------- 清单与抽样

def load_links(path: "str | os.PathLike | None" = None) -> dict:
    """读 by_uid 清单；缺失/损坏 → 空 dict（与 downloads.py 同一条「永不崩→降级」合同）。"""
    try:
        by_uid = json.loads(Path(path or _MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(by_uid, dict):
            return {}
        return {k: v for k, v in by_uid.items() if isinstance(v, dict)}
    except Exception:
        return {}


def eligible_uids(by_uid: dict) -> "dict[str, str]":
    """primary 主文件为 .h5 的数据集 → {uid: platform}（分层键；无 platform 落 unknown）。

    只认清单自述的 primary_filename / primary_download_url，不猜别的文件——烟囱口径与
    download_plan 的 primary 档一致（抽到哪个 uid，executor 下的就是这份 .h5）。
    """
    out: dict[str, str] = {}
    for uid, rec in by_uid.items():
        fn = (rec.get("primary_filename") or "").lower()
        if fn.endswith(".h5") and rec.get("primary_download_url"):
            out[uid] = rec.get("platform") or "unknown"
    return out


def _allocate(sizes: "list[int]", total: int) -> "list[int]":
    """比例分层名额：向下取整 + 最大余数法补齐/回收，每非空层至少 1（total 够分时）。纯确定性。"""
    if total <= len(sizes):   # 名额比层数还少：只保最大的几层，其余 0
        keep = set(sorted(range(len(sizes)), key=lambda j: (-sizes[j], j))[:total])
        return [1 if j in keep else 0 for j in range(len(sizes))]
    n = sum(sizes)
    raw = [total * s / n for s in sizes]
    quota = [max(1, int(x)) for x in raw]
    while sum(quota) > total:  # 取整+保底超了：从「超出自身比例最多且名额 >1」的层回收
        i = max((j for j in range(len(quota)) if quota[j] > 1),
                key=lambda j: (quota[j] - raw[j], -j))
        quota[i] -= 1
    while sum(quota) < total:  # 欠了：补给小数余量最大的层
        i = max(range(len(quota)), key=lambda j: (raw[j] - quota[j], -j))
        quota[i] += 1
    return quota


def stratified_sample(by_uid: dict, *, sample: int = DEFAULT_SAMPLE,
                      seed: int = DEFAULT_SEED) -> "list[str]":
    """按 platform 分层、层内均匀随机抽样。固定 seed → 返回逐位可复现的 uid 列表。

    名额按层规模比例分配（每非空层至少 1），保证小平台（如 Visium/Chromium 之外的层）
    在样本里也有代表性，不会被大层挤没。
    """
    strata: dict[str, list[str]] = {}
    for uid, plat in sorted(eligible_uids(by_uid).items()):
        strata.setdefault(plat, []).append(uid)
    if not strata:
        return []
    names = sorted(strata)
    sizes = [len(strata[n]) for n in names]
    total = min(max(1, int(sample)), sum(sizes))
    rng = random.Random(seed)
    picked: list[str] = []
    for name, quota in zip(names, _allocate(sizes, total)):
        pool = strata[name]
        picked.extend(rng.sample(pool, min(quota, len(pool))))
    return picked


# ---------------------------------------------------------------- scanpy 闸与 reader 接缝

def require_scanpy():
    """可选依赖闸（fail-closed）：扫得到 scanpy 返回模块，否则 None。测试 monkeypatch 此函数。"""
    try:
        import scanpy
        return scanpy
    except ImportError:
        return None


def _read_10x_h5(path: Path):
    """默认 .h5 reader：惰性 import scanpy——构建 readers 表本身不触发导入（测试可整个换掉）。"""
    import scanpy
    return scanpy.read_10x_h5(str(path))


def default_readers() -> "dict[str, Callable]":
    """文件后缀（小写）→ 加载函数。不在表里的后缀 = skipped_unsupported，不硬解。"""
    return {".h5": _read_10x_h5}


# ---------------------------------------------------------------- 一次冒烟

def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_smoke(uids: Sequence[str], dest: "str | os.PathLike", *,
              readers: "dict[str, Callable] | None" = None,
              include_flagged: bool = False, workers: int = 1,
              opener: Callable = DE._open_stream, sleep: Callable = time.sleep) -> dict:
    """抽样下载 → 按类型分发真加载 → 结构化报告（schema biodata-loadsmoke/v0）。

    下载完全交给 download_executor（opener/sleep 是它的网络/时钟接缝，测试注入即全程禁网）；
    readers 是加载接缝，默认表只在真正读 .h5 时才 import scanpy。
    """
    if readers is None:
        readers = default_readers()
    prov = DE.provision(list(uids), dest, scope=DP.SCOPE_PRIMARY,
                        include_flagged=include_flagged, workers=workers,
                        opener=opener, sleep=sleep)
    out_root = Path(prov.out_dir)

    results: list[dict] = []
    for fr in prov.results:
        entry = {
            "dataset_uid": fr.dataset_uid, "url": fr.url, "filename": fr.filename,
            "download_status": fr.status, "http_status": fr.http_status,
            "bytes_downloaded": fr.bytes_downloaded,
            "md5_expected": fr.md5_expected, "md5_actual": fr.md5_actual,
            "load_status": None, "shape": None, "n_obs": None, "n_vars": None,
            "error": None, "note": None,
        }
        if fr.status in (DE.STATUS_SKIPPED_FLAGGED, DE.STATUS_REJECTED):
            entry["load_status"] = DOWNLOAD_FAILED
            entry["note"] = f"下载未执行（{fr.status}）：{fr.error or '巡检旗标文件默认跳过'}"
        elif fr.status == DE.STATUS_UNREACHABLE:
            entry["load_status"] = DOWNLOAD_FAILED
            entry["error"] = fr.error
        else:
            ext = Path(fr.filename).suffix.lower()
            reader = readers.get(ext)
            if reader is None:
                entry["load_status"] = SKIPPED_UNSUPPORTED
                entry["note"] = f"类型 {ext or '(无扩展名)'} 暂不支持真加载：诚实降级，不硬解格式。"
            elif fr.saved_as and fr.saved_as.endswith(".corrupt"):
                # md5/大小核对没过的文件不下结论—— corrupt 证据留盘，加载维度如实跳过。
                entry["load_status"] = SKIPPED_UNSUPPORTED
                entry["note"] = "下载核对不通过（已改名 .corrupt 留证据）：不加载损坏文件，load 维不结论。"
            else:
                try:
                    adata = reader(out_root / (fr.saved_as or ""))
                    entry["shape"] = [int(x) for x in adata.shape]
                    entry["n_obs"] = int(adata.n_obs)
                    entry["n_vars"] = int(adata.n_vars)
                    entry["load_status"] = LOADED
                except Exception as e:  # 读入报错是确定答案：记 load_failed，错误摘要留类型+截断信息
                    entry["load_status"] = LOAD_FAILED
                    entry["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        results.append(entry)

    counts = {s: 0 for s in LOAD_STATUSES}
    for r in results:
        counts[r["load_status"]] += 1
    return {
        "schema": SCHEMA,
        "dataset_uids": list(uids),
        "dest": prov.out_dir,
        "include_flagged": include_flagged,
        "started_at": prov.started_at,
        "finished_at": _utc_now(),
        "counts": counts,
        "results": results,
    }


def summary_zh(report: dict) -> str:
    """人类可读摘要（与 JSON 报告同一份数据）。"""
    c = report["counts"]
    lines = [
        f"本次冒烟：{len(report['dataset_uids'])} 个数据集，每个抽 primary 主文件。",
        f"  真加载成功 {c[LOADED]}；加载失败 {c[LOAD_FAILED]}（下载核对通过但读入报错）。",
        f"  类型不支持跳过 {c[SKIPPED_UNSUPPORTED]}；下载未成 {c[DOWNLOAD_FAILED]}。",
    ]
    if c[LOADED]:
        lines.append(f"文件已保存到：{report['dest']}")
    if c[LOAD_FAILED] + c[DOWNLOAD_FAILED]:
        lines.append("有失败项：用 --out 落 JSON 报告后可交 record_provision_results.py 回写台账。")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv: "Sequence[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="加载抽样冒烟：抽样真下载 primary .h5 并用 scanpy 真加载，产出 load 维实测报告。")
    ap.add_argument("--dest", required=True,
                    help="下载目标目录（必须绝对路径；不存在则创建；受保护区 fail-closed 拒绝）")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help=f"抽样数据集数（默认 {DEFAULT_SAMPLE}，按 platform 分层均匀抽样）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help=f"随机种子（默认 {DEFAULT_SEED}，固定即可复现同一份样本）")
    ap.add_argument("--uids", default="", help="逗号分隔的 dataset_uid；给了就不抽样、逐个实测")
    ap.add_argument("--workers", type=int, default=1, help=f"下载小并发（1-{DE.MAX_WORKERS}，默认 1）")
    ap.add_argument("--include-flagged", action="store_true",
                    help="连同巡检旗标（dead/size_mismatch）文件一起下（默认跳过）")
    ap.add_argument("--out", default="",
                    help="JSON 报告落盘路径（随后用 record_provision_results.py --report 回写台账）")
    ap.add_argument("--json", action="store_true", help="只输出机器可读 JSON 报告")
    args = ap.parse_args(argv)

    if require_scanpy() is None:
        print("[load_smoke] 缺少可选依赖 scanpy：真加载做不了，fail-closed 退出（没下载任何文件）。\n"
              "  安装：py -m pip install -r requirements/requirements-loadsmoke.txt\n"
              "  （scanpy 是冒烟专用可选依赖，刻意不进 requirements.txt / requirements-ci.txt）",
              file=sys.stderr)
        return 2

    by_uid = load_links()
    if not by_uid:
        print(f"[load_smoke] 文件清单缺失或损坏：{_MANIFEST}", file=sys.stderr)
        return 2
    if args.uids:
        uids = [u.strip() for u in args.uids.split(",") if u.strip()]
    else:
        uids = stratified_sample(by_uid, sample=args.sample, seed=args.seed)
    if not uids:
        print("[load_smoke] 没有可抽样的数据集（清单里没有任何 primary 为 .h5 的记录）。",
              file=sys.stderr)
        return 2
    print(f"[load_smoke] 目标 {len(uids)} 个数据集（sample={args.sample} seed={args.seed}）", flush=True)

    try:
        report = run_smoke(uids, args.dest, include_flagged=args.include_flagged,
                           workers=args.workers)
    except DE.ProvisionError as e:
        print(f"[load_smoke] {e.code}: {e}", file=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"[load_smoke] 报告 -> {args.out}")
    if args.json:
        print(payload)
    else:
        for r in report["results"]:
            print(f"  {r['load_status']:20s} {r['dataset_uid'][:44]:46s} "
                  f"{r['error'] or r['note'] or (str(r['shape']) if r['shape'] else '')}")
        print(summary_zh(report))
    c = report["counts"]
    return 1 if c[LOAD_FAILED] + c[DOWNLOAD_FAILED] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
