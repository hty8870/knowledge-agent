# -*- coding: utf-8 -*-
"""扫描本地实验室目录 → 与本目录文件清单比对 → 资产台账（只读、纯校验和、不联网）。

只哈希字节算 md5、绝不读数据内容；不改任何本地文件。md5 校验只对 10x 基础语料的 15119 个文件清单
可用（外部库无单文件清单，见 lab_ledger 模块头诚实边界）。

用法：
    py -3 scripts/scan_lab_assets.py <目录>                 # 人读摘要
    py -3 scripts/scan_lab_assets.py <目录> --json          # 机器可读 JSON 到 stdout
    py -3 scripts/scan_lab_assets.py <目录> --json out.json # 落地报告文件
    py -3 scripts/scan_lab_assets.py <目录> --max-files 5000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dataset_recommender.content import lab_ledger  # noqa: E402


def main() -> int:
    # Windows 默认 GBK 控制台无法编码 ✅/⚠️/❔ 等 emoji（会 UnicodeEncodeError 崩）。把 stdout 切成
    # utf-8 + errors=replace：UTF-8 终端全渲染，GBK 终端最多 emoji 变问号、绝不崩。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="扫描本地目录，与数据集文件清单比对（只读、纯 md5）。")
    ap.add_argument("directory", help="要扫描的本地目录")
    ap.add_argument("--json", nargs="?", const="-", default=None,
                     help="输出 JSON（跟文件路径则落地，否则打到 stdout）")
    ap.add_argument("--max-files", type=int, default=lab_ledger.MAX_SCAN_FILES)
    args = ap.parse_args()

    index = lab_ledger.build_manifest_index()
    scan = lab_ledger.scan_directory(args.directory, index, max_files=args.max_files)
    report = lab_ledger.build_ledger_report(scan, index)

    if args.json is not None:
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.json in ("-", ""):
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"wrote report -> {args.json}")
        return 0 if report["root_exists"] else 2

    if not report["root_exists"]:
        print(f"目录不存在或不是目录：{args.directory}")
        return 2
    mf = report["manifest"]
    print(f"扫描 {report['n_scanned']} 个文件（清单 {mf['n_files']} 文件 / {mf['n_datasets']} 数据集）")
    print(f"  ✅ md5 核验通过：{report['n_verified']}")
    print(f"  ⚠️ md5 不符（本地副本与来源声明不一致）：{report['n_md5_mismatch']}")
    print(f"  ~ 仅按文件名/大小弱匹配（md5 未确认、通用名可能张冠李戴，只作线索）：{report.get('n_weak_match', 0)}")
    print(f"  ❔ 本目录不认识：{report['n_unmatched']}")
    if report["n_unhashed"]:
        print(f"  （{report['n_unhashed']} 个巨文件跳过 md5、只按名+大小比对，未算校验）")
    if report.get("n_symlink_skipped"):
        print(f"  （跳过 {report['n_symlink_skipped']} 个符号链接文件，不跟到目录外哈希）")
    if report.get("n_unreadable"):
        print(f"  （{report['n_unreadable']} 个文件读失败/权限受限，未纳入）")
    print(f"  本地拥有已知数据集：{report['n_datasets_present']} 个")
    for d in report["datasets"][:30]:
        title = (d.get("primary_title") or "")[:50]
        print(f"    {d['dataset_uid']}: 本地 {d['local_matched']}/{d['catalog_files']} 文件"
              f"（✅{d['verified']} ⚠️{d['md5_mismatch']} ❔{d['weak_match']}） {title}")
    if report["truncated"]:
        print(f"  （达到文件上限 {args.max_files}，未扫完）")
    print("\n" + report["caveat"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
