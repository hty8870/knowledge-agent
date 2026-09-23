# -*- coding: utf-8 -*-
"""生成对外**提交包**（一个可以直接交出去的文件夹）。

和 `make_delivery.py` 的分工：
- `make_delivery.py` 负责回答「**哪些文件**可以外发」（git 已跟踪对账 + 可选附加排除清单 + 敏感词/secret 复核）；
- 本脚本负责回答「交出去的**文件夹长什么样**」，并保证这个形状是**可复现**的，而不是每次手工拼。

产出结构（客户打开压缩包第一眼看到的就是这三样）：

    biodata-agent-提交版-<日期>/
    ├─ 从这里开始.txt                 说明：装什么、双击哪个、可选桌面快捷方式
    ├─ BioData Agent 使用说明书.pdf   全部功能的完整说明
    ├─ 创建桌面快捷方式.bat           可选：在桌面生成一键启动的快捷方式
    ├─ 打开前端.bat                   双击启动（能自动找到下一级的 biodata-agent\\）
    └─ biodata-agent/                 程序本体与源码 = make_delivery 复核通过的交付集

用法（先解析真实 Python 3.10+）：
  <python> scripts/make_submission_package.py --out "C:/.../biodata-agent-提交版-" --manual "C:/.../说明书.pdf"

复核不通过（内部专名 / secret / git 未跟踪件混入）时**拒绝生成**，退出码 1。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_delivery as MD  # noqa: E402

INNER_DIR_NAME = "biodata-agent"
LAUNCHER_NAME = "打开前端.bat"
SHORTCUT_NAME = "创建桌面快捷方式.bat"
START_HERE_NAME = "从这里开始.txt"

# 放在提交包根目录的第一份文件。刻意只讲「怎么跑起来」和「去哪找别的」，
# 不重复说明书的内容——客户打开文件夹后 30 秒内需要知道的就这些。
START_HERE = """BioData Agent · 提交包
================================================================

这个文件夹里有四样东西：

  1. 打开前端.bat                    双击它就能用（Windows）
  2. 创建桌面快捷方式.bat            可选：在桌面生成一键启动图标
  3. BioData Agent 使用说明书.pdf    全部功能的完整说明，建议先翻一遍
  4. biodata-agent\\                  程序本体与源码


第一次启动
----------------------------------------------------------------
1. 确认电脑已安装 Python 3.10 或更高版本。
2. 双击「打开前端.bat」。
   首次启动会自动准备 Python 环境（约一两分钟）：优先用项目自带或本机已有的合适
   环境，都没有才新建（只在需要新装依赖时联网）。多台安装可能共用本机已有环境，
   属正常现象。
   随后它会用英文问你三个可选问题，**三个都可以直接按回车跳过**：
     Configure an AI API key now? [y/N]                要不要现在配 AI 接口（跳过不影响使用）
     Install agent execution dependencies now? [y/N]   要不要装 AI 执行依赖（约 1 分钟）
     Download the local model now? [y/N]               要不要现在下本地语义模型（下载约 3 GB，装完占磁盘约 5 GB）
   答完就自动打开浏览器。这三项之后都能再补，详见说明书 2.2.1。
3. （可选）双击「创建桌面快捷方式.bat」，桌面会出现 "BioData Agent" 图标，
   以后从桌面一键打开（快捷方式指向本文件夹，移动后重跑一次刷新）。
4. 页面地址默认是 http://127.0.0.1:7860 。
   如果 7860 已被占用，会自动改用 7861-7869 里第一个空闲的端口，
   实际地址以启动窗口里显示的那一行为准。
   如果同一版本已经在运行，会直接复用它而不重复启动；窗口里会写明
   复用的是哪一份安装，若和你想启动的不是同一份，会特别提醒。
5. 关掉启动时弹出的黑色命令行窗口，服务就停止了。

装好依赖之后，检索本身**完全离线**可用，不需要联网，也不需要任何密钥。
只有 AI 相关的可选功能（AI 重排、AI 润色说明、AI 中文导读）才需要联网。
「使用反馈」默认开启本地采集；每个本机账户首次发送前独立确认。记录先存本机——本包出厂
未配置上传通道，不会上传；若部署方另外配置了上传通道，记录经尽力过滤
（手机号/证件号/邮箱自动遮蔽、API Key 等直接标识删除，并非匿名化）后经明文 HTTP 上传到
部署方的服务器（保留 90 天），用于改进推荐排序与制作评测基准；另记 MCP 调用记录（工具名/
参数摘要/耗时与成败，本机设备级，同意后才随上传发出）。明文链路可被读取，请勿输入敏感内容。
不采集 API Key、密码和账户名。可在设置里关闭或清空当前账户尚未上传的记录；已上传数据不会
因本地清空而远程删除。


还想看什么
----------------------------------------------------------------
- 安装、命令行用法、二次开发        biodata-agent\\README.md
- 接入 AI 助手（MCP）、上传自己的数据  biodata-agent\\使用教程\\
- 拿到这个包之后怎么逐项验收          biodata-agent\\测试说明.txt
"""


def build(out_root: Path, manual_pdf: Path | None) -> int:
    files = MD.collect_delivery_files(REPO_ROOT)
    violations = MD.scan_forbidden(files, REPO_ROOT)
    secrets = MD.scan_secret_values(files, REPO_ROOT)
    if violations or secrets:
        print(f"[提交包] 拒绝生成：交付复核未通过（{len(violations)} 内部专名 / {len(secrets)} secret）。"
              "先跑 scripts/make_delivery.py --check 定位。", file=sys.stderr)
        return 1

    inner = out_root / INNER_DIR_NAME
    if out_root.exists():
        shutil.rmtree(out_root)
    inner.mkdir(parents=True)

    for src in files:
        # 仓库一级目录整理后，被迁移文件在提交包内仍按历史根目录布局落位
        # （make_delivery.delivery_arcname），客户拿到的包形状与迁移前逐位一致。
        dst = inner / MD.delivery_arcname(src.relative_to(REPO_ROOT).as_posix())
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    (out_root / START_HERE_NAME).write_text(START_HERE, encoding="utf-8")
    shutil.copy2(REPO_ROOT / "launchers" / LAUNCHER_NAME, out_root / LAUNCHER_NAME)
    shutil.copy2(REPO_ROOT / "launchers" / SHORTCUT_NAME, out_root / SHORTCUT_NAME)

    if manual_pdf is not None:
        if not manual_pdf.is_file():
            print(f"[提交包] 说明书 PDF 不存在：{manual_pdf}", file=sys.stderr)
            return 1
        shutil.copy2(manual_pdf, out_root / manual_pdf.name)

    total_bytes = sum(f.stat().st_size for f in out_root.rglob("*") if f.is_file())
    print(f"[提交包] 已生成：{out_root}")
    print(f"  {INNER_DIR_NAME}/ 内 {len(files)} 个文件")
    print(f"  根目录：{START_HERE_NAME}、{LAUNCHER_NAME}、{SHORTCUT_NAME}"
          + (f"、{manual_pdf.name}" if manual_pdf else "（未附说明书 PDF）"))
    print(f"  总大小：{total_bytes / 1048576:.1f} MiB")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="生成对外提交包文件夹")
    ap.add_argument("--out", required=True, help="提交包目标文件夹（存在则先删除重建）")
    ap.add_argument("--manual", help="使用说明书 PDF 路径；不给则不附")
    args = ap.parse_args(argv)
    try:
        return build(Path(args.out), Path(args.manual) if args.manual else None)
    except MD.GitUnavailable as exc:
        print(f"[提交包] 无法核验交付集：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
