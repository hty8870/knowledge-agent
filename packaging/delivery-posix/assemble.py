#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把发布候选包 + PDF 装配成全平台（Windows/macOS/Linux）命令行交付包。

定位：zip 版安装包——三平台通用、命令行/脚本启动、前端依赖浏览器
（Windows 图形化 webview 安装版走 Inno exe，不在本装配范围）。
解压后点一个入口脚本就全流程启动：Windows 双击 打开前端.bat、
macOS 双击 打开前端.command、Linux 终端 sh 打开前端.sh。

用法：
  <python> packaging/delivery-posix/assemble.py <rc_zip> <pdf> [-o <out_dir>] [--date <YYYY-MM-DD>]

输入：
  rc_zip  scripts/build_release.py build 产出的发布候选包（zip，含 allowlist 源码树）。
  pdf     「BioData Agent 使用说明书.pdf」的任意路径（输出时保留其原名）。

输出（out_dir 默认仓库根的 dist/）：
  biodata-agent-<YYYY-MM-DD>/
    ├─ 从这里开始.txt                三平台说明
    ├─ 打开前端.bat                  Windows 双击入口
    ├─ 打开前端.sh                   Linux/通用入口（sh 前端根定位 + exec bash 引擎）
    ├─ 打开前端.command              macOS 双击入口（内容同 .sh）
    ├─ BioData Agent 使用说明书.pdf  原样拷贝
    └─ biodata-agent/                RC zip 解出的源码树（exec 位保留）
  以及同名的 .zip（用 python zipfile 手动控制 external_attr，保证 .sh/.command 的 exec 位）。

设计：纯标准库、确定性。exec 位不从磁盘 mode 读（Windows 磁盘 mode 不可靠），而是：
  * 顶层入口脚本 .sh/.command 一律 0o100755，.bat/文本/PDF 一律 0o100644；
  * 从 RC 解出的文件复用其 zip 条目自带的 mode（build_release 已把可执行条目写成 0o100755）。
"""
from __future__ import annotations

import argparse
import datetime
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
DELIVERY_DIR_NAME_PREFIX = "biodata-agent-"
VERSION = 1

# 顶层文件的字节 mode（不依赖磁盘 mode，Windows 下磁盘 mode 不可靠）。
TOP_LEVEL_MODES = {
    "从这里开始.txt": 0o100644,
    "打开前端.bat": 0o100644,
    "打开前端.sh": 0o100755,
    "打开前端.command": 0o100755,
}
# dispatch by suffix：PDF 以及其他顶层拷贝统一 644。
TOP_LEVEL_DEFAULT_MODE = 0o100644

# RC zip 里这些条目不进交付（构建记录/内部工件，不需要随客户源码树外发）。
RC_EXCLUDED = frozenset({"release-manifest.json"})

FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def _zip_info(relative: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    info.flag_bits |= 0x800
    return info


def _mode_from_zipinfo(info: zipfile.ZipInfo, default: int = 0o100644) -> int:
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode == 0:
        return default
    return mode


def _rc_entry_modes(rc_zip: Path) -> dict[str, int]:
    """读 RC zip，返回「zip 内相对路径 -> 字节 mode」（缺失给 0o100644）。"""
    modes: dict[str, int] = {}
    with zipfile.ZipFile(rc_zip, "r") as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            # 转正斜杠，统一纯相对路径。
            relative = info.filename.replace("\\", "/")
            if relative in RC_EXCLUDED:
                continue
            modes[relative] = _mode_from_zipinfo(info)
    return modes


def _safe_join(base: Path, relative: str) -> Path:
    """把 zip 内相对路径安全映射到 base 下，拒绝逃逸/绝对路径/反斜杠。"""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ValueError(f"unsafe archive path in RC: {relative!r}")
    return base.joinpath(*pure.parts)


def build_delivery(
    rc_zip: Path,
    pdf: Path,
    out_dir: Path,
    date_str: str,
) -> Path:
    """在 out_dir 下装配并压缩 mac/Linux 交付包，返回 zip 路径。"""
    rc_zip = rc_zip.resolve()
    pdf = pdf.resolve()
    out_dir = out_dir.resolve()
    if not rc_zip.is_file():
        raise FileNotFoundError(f"RC zip not found: {rc_zip}")
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    delivery_name = f"{DELIVERY_DIR_NAME_PREFIX}{date_str}"
    delivery_dir = out_dir / delivery_name
    if delivery_dir.exists():
        shutil.rmtree(delivery_dir)
    delivery_dir.mkdir(parents=True)

    # 1) 顶层说明 + 入口脚本 + PDF（入口脚本 起在仓库内经 launchers/ 维护；
    #    交付包内仍落在顶层，布局不变）。
    readme = Path(__file__).with_name("从这里开始.txt")
    shutil.copyfile(readme, delivery_dir / "从这里开始.txt")
    shutil.copyfile(REPO_ROOT / "launchers" / "打开前端.bat", delivery_dir / "打开前端.bat")
    shutil.copyfile(REPO_ROOT / "launchers" / "打开前端.sh", delivery_dir / "打开前端.sh")
    shutil.copyfile(REPO_ROOT / "launchers" / "打开前端.command", delivery_dir / "打开前端.command")
    shutil.copyfile(pdf, delivery_dir / pdf.name)

    # 2) 解出源码树（复用 RC 里的 mode，保留 .sh/.command 的 exec 位）。
    source_dir = delivery_dir / "biodata-agent"
    source_dir.mkdir()
    modes = _rc_entry_modes(rc_zip)
    with zipfile.ZipFile(rc_zip, "r") as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            relative = info.filename.replace("\\", "/")
            if relative in RC_EXCLUDED:
                continue
            target = _safe_join(source_dir, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(info.filename))
            mode = modes.get(relative, 0o100644)
            # 尽量落盘 exec 位（POSIX 有效；Windows 尽力而为）。
            try:
                target.chmod(mode & 0o7777)
            except OSError:
                pass

    # 3) 打成 exec 位保留的 zip。
    archive = out_dir / f"{delivery_name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zip_out:
        # 顶层文件（mode 由 TOP_LEVEL_MODES / 默认决定）。
        for name in (
            "从这里开始.txt",
            "打开前端.bat",
            "打开前端.sh",
            "打开前端.command",
            pdf.name,
        ):
            path = delivery_dir / name
            mode = TOP_LEVEL_MODES.get(name, TOP_LEVEL_DEFAULT_MODE)
            zip_out.writestr(_zip_info(name, mode), path.read_bytes())
        # 源码树（mode 复用 RC 记录；目录条目写 0o40755）。
        for relative in sorted(modes):
            path = _safe_join(source_dir, relative)
            if not path.is_file():
                continue
            zip_out.writestr(
                _zip_info(f"biodata-agent/{relative}", modes[relative]),
                path.read_bytes(),
            )
    return archive


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble the all-platform (Windows/macOS/Linux) command-line delivery package.")
    parser.add_argument("rc_zip", type=Path, help="path to the release candidate zip")
    parser.add_argument("pdf", type=Path, help="path to 'BioData Agent 使用说明书.pdf'")
    parser.add_argument("-o", "--out-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    date_str = args.date or datetime.date.today().isoformat()
    try:
        archive = build_delivery(args.rc_zip, args.pdf, args.out_dir, date_str)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
