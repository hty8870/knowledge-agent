# -*- coding: utf-8 -*-
"""把下载计划渲染成用户拿到手就能跑的一组文件。**只生成文本、不写盘、不联网。**

## 两个脚本刻意长成这样

两个 runner 都是**薄读取器**：URL、md5、字节数只存在于 `manifest.tsv` 一个文件里，
脚本本身不内联任何链接列表。这样用户可以先看清单再决定跑不跑，也可以只改清单不碰脚本。

安全上守死几条，因为这是本项目第一个「生成出去、在别人机器上执行」的产物：

- **默认只打印计划，不下载**。要加 `--go` / `-Go` 才真的开始下。
- **只放行 https，且主机名必须在清单头部声明的白名单里**，其余整行跳过并计数。
  跳转也钉死在 https（`--proto =https --proto-redir =https`），杜绝被跳到明文 http 上；
  跳转之后落到哪个主机由来源站点决定，白名单只管住我们**主动发起**的那一次请求——这一条
  必须照实说，不能写成「全程主机都在白名单里」。
- 绝不 `eval` / `Invoke-Expression` / 管道进 shell、绝不自更新、绝不读任何环境变量密钥或 `.env`。
- 先下到 `.part` 再改名；**下完必须回头核对**：有 md5 就比 md5，没有 md5 但来源声明了大小
  就比字节数，两者都不符一律改名成 `.corrupt` 留证据，不覆盖也不删除。
  核不动的（本机没有可用的 md5 工具、来源既没给 md5 也没给大小）如实记成 `unverified`，
  **绝不记成 ok**——「下完了」和「核对过了」是两件事。

## 编码不是小事

`.ps1` 用 CRLF + UTF-8 BOM（Windows PowerShell 5.1 读无 BOM 的 UTF-8 会把中文变成乱码，
用户看到的是一屏问号）；`.sh` 用 LF 无 BOM；其余文本 LF 无 BOM。
"""
from __future__ import annotations

import json
from typing import Any

from . import download_plan as DP

SCHEMA_MANIFEST = "biodata-download-plan/v1"

_TSV_COLUMNS = ("dataset_uid", "safe_uid", "source", "tier", "filename", "filename_derived",
                "safe_name", "size_bytes", "md5", "verify", "flag_kind", "reason", "url")


def human_bytes(size: "int | None") -> str:
    if not size:
        return "未知大小"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


#: 与本次选中了几条**无关**的那半句。面板要在用户勾勾选选的过程中一直显示它，
#: 而带数字的那半句一勾选就过期——两半必须分开，否则面板只能在「不显示」和「显示旧数字」里二选一。
PRIMARY_ONLY_POLICY_ZH = ("本包为每个数据集只列出 1 个代表性主文件（通常是处理后的表达矩阵）。"
                          "需要全部文件请到数据集页面点「查看全部文件」。")

#: scope=all 的同位政策句（2026-08-08 约束放松批 ，评审裁决：scope=all 已是公开能力
#: 产物却永远声称「只取主文件」= 假话——声明必须跟着 scope 走）。
ALL_FILES_POLICY_ZH = "本包为每个数据集列出来源清单里的全部文件。"


def primary_only_sentence(plan: dict) -> str:
    """主文件/全文件声明。四处必现（面板、README、待办、脚本结尾），是同一句。

    这句话是本产物最重要的一句：用户搜的是「含 FASTQ 的数据」，拿到的包里却一个 FASTQ 都没有，
    而脚本还打印「全部通过」——那是最严重的一种「技术上没说错、实际把人骗了」。

    体积那个数字曾经挂在「其中 FASTQ M 个」后面，中文读起来就是「这 M 个 FASTQ 约 X」；
    但 X 是**全部**被排除文件（含 BAM、cloupe、molecule_info…）的合计。数字本身没错，
    位置放错了就等于虚报。所以体积单独成句，主语写死成「这些没有列入的文件」。

    2026-08-08 K8：声明按 plan.scope 分岔——scope=all 时「只取主文件」一个字都不能出现。
    """
    est = plan.get("estimate", {})
    n_ds = est.get("n_datasets_selected", 0)
    n_sel = est.get("n_files_selected", 0)
    if str(plan.get("scope") or DP.SCOPE_PRIMARY) != DP.SCOPE_PRIMARY:
        return (f"本包为每个数据集列出来源清单里的全部文件："
                f"本次 {n_ds} 个数据集共 {n_sel} 个文件，"
                f"合计约 {human_bytes(est.get('bytes_selected_lower_bound'))}（清单声明值的下限）。")
    n_fastq = est.get("n_fastq_files_excluded", 0)
    excluded = int(est.get("n_files_excluded") or 0)
    text = (f"本包为每个数据集只列出 1 个代表性主文件（通常是处理后的表达矩阵）："
            f"本次 {n_ds} 个数据集共 {n_sel} 个文件")
    if excluded > 0:
        text += f"，来源清单里另有 {excluded} 个文件没有列入"
        if n_fastq > 0:
            text += f"（其中原始测序数据 FASTQ 等 {n_fastq} 个）"
        text += f"，这些没有列入的文件合计约 {human_bytes(est.get('bytes_excluded_lower_bound'))}"
    text += "。需要全部文件请到数据集页面点「查看全部文件」。"
    return text


def render_manifest_tsv(plan: dict, meta: dict) -> str:
    hosts = ",".join(plan.get("allowed_hosts") or [])
    lines = [
        "# 这是给脚本读的清单；人请看同目录的 file-list.md。",
        f"# plan_token: {meta.get('plan_token', '')}",
        f"# snapshot_id: {meta.get('snapshot_id', '')}  content_digest: {meta.get('content_digest', '')}",
        f"# retrieval_date: {meta.get('retrieval_date', '')}",
        f"# 巡检快照: {plan.get('inspection', {}).get('snapshot_date', '未记录')}"
        f"（{plan.get('inspection', {}).get('scope_zh', '')}）",
        f"# allowed_hosts: {hosts}",
        "# md5 语义：来源公布的声明值，本工具没有重新算过。",
        "\t".join(_TSV_COLUMNS),
    ]
    # 可读性：按数据集分组——同一数据集的文件行连续出现，组前插一行 `# ===== 编号 · 标题 =====`
    # 注释。两个脚本都 `grep -v '^#'` 跳过注释行，格式零影响；人眼（记事本/Excel）能直接对上归属。
    by_uid = {it["dataset_uid"]: it for it in plan.get("items", [])}
    current_uid = None
    for row in plan.get("rows", []):
        if row["dataset_uid"] != current_uid:
            current_uid = row["dataset_uid"]
            item = by_uid.get(current_uid, {})
            title = str(item.get("dataset_name") or current_uid).replace("\t", " ").replace("\n", " ")
            lines.append(f"# ===== {current_uid} · {title} =====")
        lines.append("\t".join(str(x) for x in (
            row["dataset_uid"], row["safe_uid"], row["source"], row["tier"],
            row["filename"], "1" if row["filename_derived"] else "0", row["safe_name"],
            row["bytes"] if row["bytes"] else "-", row["md5sum"] or "-", row["verify"],
            row["flag_kind"] or "-", (row["flag_reason_zh"] or "-").replace("\t", " "),
            row["download_url"],
        )))
    return "\n".join(lines) + "\n"


def render_manifest_json(plan: dict, meta: dict) -> str:
    payload = {
        "schema": SCHEMA_MANIFEST,
        "plan_token": meta.get("plan_token", ""),
        "snapshot_id": meta.get("snapshot_id", ""),
        "content_digest": meta.get("content_digest", ""),
        "retrieval_date": meta.get("retrieval_date", ""),
        "scope": plan.get("scope", DP.SCOPE_PRIMARY),
        "scope_note_zh": primary_only_sentence(plan),
        "ledger_snapshot_date": plan.get("inspection", {}).get("snapshot_date", ""),
        "allowed_hosts": plan.get("allowed_hosts", []),
        "totals": plan.get("estimate", {}),
        "tiers": plan.get("tiers", []),
        "rows": plan.get("rows", []),
        "manual": plan.get("manual", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def render_file_list_md(plan: dict) -> str:
    lines = ["# 这一包里有哪些文件", "", primary_only_sentence(plan), "",
             "下面按数据集分组：每组标题 = 数据集编号 + 数据集标题，文件列在组内。",
             "脚本读的是同目录的 manifest.tsv（格式与这里一致：先分组、再逐文件）。", ""]
    by_uid = {it["dataset_uid"]: it for it in plan.get("items", [])}
    current_uid = None
    for row in plan.get("rows", []):
        if row["dataset_uid"] != current_uid:
            current_uid = row["dataset_uid"]
            item = by_uid.get(current_uid, {})
            lines += ["", f"## {_cell(current_uid)} · {_cell(item.get('dataset_name') or current_uid)}"
                      f"（来源：{_cell(item.get('source') or '—')}）", "",
                      "| 文件 | 大小 | 能核对到什么 |", "|---|---|---|"]
        verify = {"md5": "文件大小 + 校验和", "size": "只能核对文件大小", "none": "无法核验"}[row["verify"]]
        if row["flag_kind"]:
            verify += "（有已知问题，默认跳过）"
        lines.append("| {} | {} | {} |".format(
            _cell(row["filename"]), human_bytes(row["bytes"]), verify))
    if plan.get("manual"):
        lines += ["", "## 脚本下不了的（需要你自己去页面上取）", ""]
        for entry in plan["manual"]:
            lines.append(f"- {_cell(entry['dataset_name'])}（{_cell(entry['source'])}）："
                         f"{entry['why_zh']} 页面：{entry['page_url'] or '未提供'}")
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def render_start_here(plan: dict) -> str:
    est = plan.get("estimate", {})
    return f"""BioData Agent · 数据下载包
================================================================

这个压缩包里**没有任何数据文件**——里面是清单、下载脚本和引文（全是文本）。
真正的数据文件需要你自己下载，按下面的三步来。

第 1 步 · 先弄明白这个包里有什么
  这是「任务包」：datasets.md 列出收录的数据集，file-list.md 按数据集分组
  列出每个数据集可下载的文件，download.sh / download.ps1 是下载脚本，其余是
  FAIR 自检、待办清单与引文。**包里没有数据文件不是丢了东西，是设计如此。**

第 2 步 · 想直接下载真实数据？
  回到 BioData Agent 界面，在「下载这批数据」面板里点「下载勾选的数据集文件」，
  浏览器会直接开始下载真实文件（每个数据集取代表性主文件；进度与取消在浏览器
  自带的下载管理里，Ctrl+J 可看）。本压缩包的脚本只是那条路的兜底——两种方式
  二选一即可。

第 3 步 · 要自己跑脚本的话
  · 每个文件属于哪个数据集：file-list.md 按数据集分组，每组标题 = 数据集编号 + 标题；
    manifest.tsv 每行的第一列就是数据集编号，脚本也按编号把文件下进 data/<编号>/。
  · 怎么跑（先把整个压缩包解压到一个文件夹，再在解压出的文件夹里运行）：
    Windows：按住 Shift 右键 →「在此处打开 PowerShell 窗口」→
      powershell -ExecutionPolicy Bypass -File .\\download.ps1       （只看计划，不下载）
      powershell -ExecutionPolicy Bypass -File .\\download.ps1 -Go   （真的开始下载）
    macOS / Linux（终端 cd 到这个文件夹）：
      sh download.sh         （只看计划，不下载）
      sh download.sh --go    （真的开始下载）
  · 校验是什么意思：有校验和的文件下完会比对 md5；只有大小的比对字节数；
    两者都核不动的会如实记成 unverified，绝不冒充「核对通过」。

本次计划：{est.get('n_datasets_selected', 0)} 个数据集 · {est.get('n_files_selected', 0)} 个文件 ·
真实数据共约 {human_bytes(est.get('bytes_selected_lower_bound'))}。

{primary_only_sentence(plan)}

其它文件都是什么：
  README.md            这一包的完整说明
  datasets.md          检索到的数据集清单
  file-list.md         给人看的文件清单（按数据集分组）
  manifest.tsv         给脚本读的文件清单（脚本只读这一个）
  todo.md              本工具帮不上、需要你自己确认的事
  fair_report.md       每个数据集的复用就绪度自检
  citations.ris / .bib 引文，可直接导进文献管理软件
  reuse_provenance.md  可放进论文数据可用性声明的英文段落
  provenance.json      这一包是怎么来的（检索条件、目录快照、口径）
"""


_SH = r'''#!/bin/sh
# BioData Agent 下载脚本（macOS / Linux）
# 只读 manifest.tsv，不内联任何链接；默认只打印计划，加 --go 才真的下载。
set -u
HERE="$(dirname "$0")"
MANIFEST="$HERE/manifest.tsv"
LOGS="$HERE/logs"
OUT="$HERE/data"
GO=0
INCLUDE_FLAGGED=0
for arg in "$@"; do
  case "$arg" in
    --go) GO=1 ;;
    --include-flagged) INCLUDE_FLAGGED=1 ;;
    --out=*) OUT="${arg#--out=}" ;;
    -h|--help)
      echo "用法: sh download.sh [--go] [--out=目录] [--include-flagged]"
      echo "  不加 --go 只打印计划，不下载。"
      echo "  --include-flagged 连同「巡检发现有问题」的文件一起下（默认跳过）。"
      exit 0 ;;
  esac
done

if [ ! -f "$MANIFEST" ]; then
  echo "没找到 manifest.tsv。如果你是直接在压缩包里运行的，请先把整个压缩包解压到一个文件夹，再运行这个脚本。"
  exit 2
fi
HEADER=$(grep -v '^#' "$MANIFEST" | head -1)
case "$HEADER" in
  dataset_uid*) : ;;
  *) echo "manifest.tsv 的格式和生成时不一样，通常是被 Excel 打开并保存过。请重新生成，或用记事本查看。"; exit 2 ;;
esac
# 一条下载命令都没有是**正常情况**（这一批数据集可能全都只有页面地址）。
# 必须在 allowed_hosts 之前判，否则空清单会被报成「没有 allowed_hosts 声明」——
# 清单其实好好的，用户会以为文件坏了。
NROWS=$(grep -v '^#' "$MANIFEST" | tail -n +2 | grep -c '[^[:space:]]' || true)
if [ "${NROWS:-0}" -eq 0 ]; then
  echo "这一包里没有可以用脚本下载的文件。"
  echo "这不表示这些数据集下不了，而是本工具没有拿到它们的文件级直链。"
  echo "请看同目录的 file-list.md 与 todo.md，按上面的页面地址自行获取。"
  exit 0
fi
ALLOWED=$(grep '^# allowed_hosts:' "$MANIFEST" | head -1 | sed 's/^# allowed_hosts: *//')
if [ -z "$ALLOWED" ]; then
  echo "manifest.tsv 里没有 allowed_hosts 声明，为安全起见不下载。"
  exit 2
fi

# 本机有哪个 md5 工具。macOS 自带的是 md5（不是 md5sum），少判这一条会让
# 「有 md5 可比」的文件在 macOS 上被静默跳过核验——而包里写着「可下载并核对校验和」。
md5_of() {
  if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | cut -d' ' -f1
  elif command -v md5 >/dev/null 2>&1; then md5 -q "$1"
  elif command -v openssl >/dev/null 2>&1; then openssl md5 "$1" | sed 's/^.*= *//'
  else return 1
  fi
}

mkdir -p "$LOGS" 2>/dev/null
[ "$GO" -eq 1 ] && mkdir -p "$OUT" 2>/dev/null
REPORT="$LOGS/report.tsv"
ROWFILE="$LOGS/.rows.$$"
grep -v '^#' "$MANIFEST" | tail -n +2 > "$ROWFILE"
# 只有真的要下载时才清空报告。`sh download.sh`（只看计划）把上一次的下载记录抹掉，
# 用户会以为那次下载没发生过。
[ "$GO" -eq 1 ] && : > "$REPORT"
OK=0; SKIP=0; FAIL=0; REJECT=0; UNVERIFIED=0; FLAGGED=0; PLANNED=0; NOTOOL=0

# 从文件重定向、不走管道：`grep ... | while` 会把整个循环放进子 shell，
# 循环里累加的计数出了循环全部归零——统计量算了等于没算。
while IFS='	' read -r uid safe_uid source tier filename derived safe_name size md5 verify flag reason url; do
  [ -z "${url:-}" ] && continue
  PLANNED=$((PLANNED+1))
  host=$(printf '%s' "$url" | sed -e 's|^https://||' -e 's|/.*$||' -e 's|:.*$||')
  case "$url" in https://*) : ;; *) echo "跳过（不是 https）：$filename"; REJECT=$((REJECT+1)); continue ;; esac
  case ",$ALLOWED," in *",$host,"*) : ;; *) echo "跳过（主机不在清单声明的白名单里）：$host"; REJECT=$((REJECT+1)); continue ;; esac
  if [ "$flag" != "-" ] && [ "$INCLUDE_FLAGGED" -eq 0 ]; then
    echo "跳过（巡检发现有问题，加 --include-flagged 才下）：$filename"
    FLAGGED=$((FLAGGED+1)); continue
  fi
  dir="$OUT/$safe_uid"; target="$dir/$safe_name"
  if [ "$GO" -eq 0 ]; then
    echo "将下载：$safe_uid/$safe_name  ($size 字节)"
    continue
  fi
  mkdir -p "$dir"
  if [ -f "$target" ]; then
    have=$(wc -c < "$target" | tr -d ' ')
    if [ "$size" != "-" ] && [ "$have" = "$size" ]; then
      echo "已存在，跳过：$safe_name"; SKIP=$((SKIP+1)); continue
    fi
  fi
  echo "下载中：$safe_uid/$safe_name"
  # --proto/--proto-redir 把首次请求和后续跳转都钉死在 https，杜绝被跳到明文 http。
  if curl -fL --proto '=https' --proto-redir '=https' --retry 3 --retry-delay 2 -C - -o "$target.part" "$url"; then
    mv -f "$target.part" "$target"
    # 下完必须回头核对。以前这里只在有 md5 且本机有 md5sum 时才核，其余一律记 ok——
    # 「只能比对字节数」那一档从来没有比对过任何字节。
    verdict=""
    if [ "$md5" != "-" ]; then
      got=$(md5_of "$target" 2>/dev/null) || got=""
      if [ -n "$got" ]; then
        if [ "$got" = "$md5" ]; then verdict="ok"; else verdict="md5_mismatch"; fi
      else
        NOTOOL=$((NOTOOL+1)); verdict="no_md5_tool"
      fi
    fi
    if [ -z "$verdict" ] || [ "$verdict" = "no_md5_tool" ]; then
      if [ "$size" != "-" ] && [ -n "$size" ]; then
        have=$(wc -c < "$target" | tr -d ' ')
        if [ "$have" = "$size" ]; then
          if [ "$verdict" = "no_md5_tool" ]; then verdict="size_ok_no_md5_tool"; else verdict="size_ok"; fi
        else
          echo "文件大小与来源声明不符（应为 $size 字节，实得 $have 字节）"
          verdict="size_mismatch"
        fi
      elif [ "$verdict" = "no_md5_tool" ]; then verdict="unverified_no_md5_tool"
      else verdict="unverified"
      fi
    fi
    case "$verdict" in
      ok|size_ok)
        OK=$((OK+1)) ;;
      md5_mismatch|size_mismatch)
        echo "核对不通过，已改名保留证据：$safe_name.corrupt"
        mv -f "$target" "$target.corrupt"; FAIL=$((FAIL+1)) ;;
      *)
        echo "已下载，但本机无法核对这个文件（$verdict）：$safe_name"
        UNVERIFIED=$((UNVERIFIED+1)) ;;
    esac
    printf '%s\t%s\t%s\n' "$uid" "$safe_name" "$verdict" >> "$REPORT"
  else
    echo "下载失败：$safe_name"; FAIL=$((FAIL+1))
    printf '%s\t%s\t%s\n' "$uid" "$safe_name" "download_failed" >> "$REPORT"
  fi
done < "$ROWFILE"
rm -f "$ROWFILE"

echo ""
if [ "$GO" -eq 0 ]; then
  echo "以上只是计划，没有下载任何文件（共 $PLANNED 条）。要真的开始下载，请运行："
  echo "  sh download.sh --go"
  exit 0
fi
echo "本次：核对通过 $OK，已存在跳过 $SKIP，未能核对 $UNVERIFIED，失败或不符 $FAIL，"
echo "      按巡检结果跳过 $FLAGGED，按安全规则拒绝 $REJECT（计划 $PLANNED 条）。"
if [ "$NOTOOL" -gt 0 ]; then
  echo "其中 $NOTOOL 个文件带 md5，但这台机器上找不到 md5sum / md5 / openssl，校验和没有核过。"
fi
# 一个文件都没落地却打印「文件已保存到 …」，是在报告一件没发生的事。
if [ $((OK+SKIP+UNVERIFIED)) -gt 0 ]; then
  echo "文件已保存到：$OUT"
else
  echo "没有任何文件成功保存。"
fi
echo "__PRIMARY_ONLY__"
echo "本工具没有下载过这些文件，也没有在你的机器上测试过这个脚本。"
echo "详情见 logs/report.tsv。"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
'''

_PS1 = r'''# BioData Agent 下载脚本（Windows PowerShell）
# 只读 manifest.tsv，不内联任何链接；默认只打印计划，加 -Go 才真的下载。
param(
    [switch] $Go,
    [string] $Out = "",
    [switch] $IncludeFlagged
)
$ErrorActionPreference = 'Stop'
# 兼容有人照着 macOS 那条命令敲 --go
foreach ($a in $args) {
    if ($a -eq '--go') { $Go = $true; Write-Host '已按 -Go 处理。' }
    if ($a -eq '--include-flagged') { $IncludeFlagged = $true }
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifest = Join-Path $here 'manifest.tsv'
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Host '没找到 manifest.tsv。如果你是直接在压缩包里双击运行的，请先把整个压缩包解压到一个文件夹，再运行这个脚本。'
    exit 2
}
if ($Out -eq '') { $Out = Join-Path $here 'data' }
$logs = Join-Path $here 'logs'
$lines = Get-Content -LiteralPath $manifest -Encoding UTF8
$allowedLine = $lines | Where-Object { $_ -like '# allowed_hosts:*' } | Select-Object -First 1
# @(...) 强制成数组。只剩一行时 Where-Object 返回的是一个字符串，$data[0] 会取到**首字符**，
# 对 [char] 调 StartsWith 直接抛 .NET 异常（$ErrorActionPreference='Stop' 下当场中止）。
$data = @($lines | Where-Object { -not $_.StartsWith('#') })
if ($data.Count -lt 1 -or -not $data[0].StartsWith('dataset_uid')) {
    Write-Host 'manifest.tsv 的格式和生成时不一样，通常是被 Excel 打开并保存过。请重新生成任务包，或用记事本查看。'
    exit 2
}
# 一条下载命令都没有是正常情况，不是清单坏了。必须在 allowed_hosts 之前判。
$rows = @($data | Select-Object -Skip 1 | Where-Object { $_.Trim() })
if ($rows.Count -lt 1) {
    Write-Host '这一包里没有可以用脚本下载的文件。'
    Write-Host '这不表示这些数据集下不了，而是本工具没有拿到它们的文件级直链。'
    Write-Host '请看同目录的 file-list.md 与 todo.md，按上面的页面地址自行获取。'
    exit 0
}
if (-not $allowedLine) { Write-Host 'manifest.tsv 里没有 allowed_hosts 声明，为安全起见不下载。'; exit 2 }
$allowed = ($allowedLine -replace '^# allowed_hosts:\s*', '').Split(',') | Where-Object { $_ }
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    Write-Host '没找到 curl.exe（Windows 10 1803 以后自带）。本脚本只用 curl.exe 下载，不改用其它方式，以免绕过断点续传与校验。'
    exit 1
}

$ok = 0; $skip = 0; $fail = 0; $reject = 0; $unverified = 0; $flagged = 0; $planned = 0
$report = @()
try {
    foreach ($line in $rows) {
        $c = $line.Split("`t")
        if ($c.Count -lt 13) { continue }
        $uid = $c[0]; $safeUid = $c[1]; $filename = $c[4]; $safeName = $c[6]
        $size = $c[7]; $md5 = $c[8]; $flag = $c[10]; $url = $c[12]
        $planned += 1
        if (-not $url.StartsWith('https://')) { Write-Host "跳过（不是 https）：$filename"; $reject += 1; continue }
        $host_ = ([Uri]$url).Host
        if ($allowed -notcontains $host_) { Write-Host "跳过（主机不在清单声明的白名单里）：$host_"; $reject += 1; continue }
        if ($flag -ne '-' -and -not $IncludeFlagged) {
            Write-Host "跳过（巡检发现有问题，加 -IncludeFlagged 才下）：$filename"; $flagged += 1; continue
        }
        $dir = Join-Path $Out $safeUid
        $target = Join-Path $dir $safeName
        if (-not $Go) { Write-Host "将下载：$safeUid\$safeName  ($size 字节)"; continue }
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if (Test-Path -LiteralPath $target) {
            $have = (Get-Item -LiteralPath $target).Length
            if ($size -ne '-' -and "$have" -eq $size) { Write-Host "已存在，跳过：$safeName"; $skip += 1; continue }
        }
        Write-Host "下载中：$safeUid\$safeName"
        # --proto/--proto-redir 把首次请求和后续跳转都钉死在 https，杜绝被跳到明文 http。
        & curl.exe -fL --proto '=https' --proto-redir '=https' --retry 3 --retry-delay 2 -C - -o "$target.part" $url
        if ($LASTEXITCODE -ne 0) {
            Write-Host "下载失败：$safeName"; $fail += 1
            $report += "$uid`t$safeName`tdownload_failed"; continue
        }
        Move-Item -LiteralPath "$target.part" -Destination $target -Force
        # 下完必须回头核对：有 md5 比 md5，没 md5 但来源声明了大小就比字节数。
        # 「只能核对文件大小」那一档以前从来没有比对过任何字节，却照样记 ok。
        $verdict = ''
        if ($md5 -ne '-') {
            $got = (Get-FileHash -LiteralPath $target -Algorithm MD5).Hash.ToLower()
            if ($got -eq $md5.ToLower()) { $verdict = 'ok' } else { $verdict = 'md5_mismatch' }
        } elseif ($size -ne '-' -and $size) {
            $have = (Get-Item -LiteralPath $target).Length
            if ("$have" -eq $size) { $verdict = 'size_ok' }
            else {
                Write-Host "文件大小与来源声明不符（应为 $size 字节，实得 $have 字节）"
                $verdict = 'size_mismatch'
            }
        } else { $verdict = 'unverified' }
        if ($verdict -eq 'ok' -or $verdict -eq 'size_ok') { $ok += 1 }
        elseif ($verdict -eq 'unverified') {
            Write-Host "已下载，但来源既没给校验和也没给大小，无法核对：$safeName"; $unverified += 1
        } else {
            Write-Host "核对不通过，已改名保留证据：$safeName.corrupt"
            Move-Item -LiteralPath $target -Destination "$target.corrupt" -Force
            $fail += 1
        }
        $report += "$uid`t$safeName`t$verdict"
    }
}
finally {
    if ($Go) {
        if (-not (Test-Path -LiteralPath $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }
        Set-Content -LiteralPath (Join-Path $logs 'report.tsv') -Value $report -Encoding UTF8
    }
}

Write-Host ''
if (-not $Go) {
    Write-Host "以上只是计划，没有下载任何文件（共 $planned 条）。要真的开始下载，请运行："
    Write-Host '  powershell -ExecutionPolicy Bypass -File .\download.ps1 -Go'
} else {
    Write-Host "本次：核对通过 $ok，已存在跳过 $skip，未能核对 $unverified，失败或不符 $fail，"
    Write-Host "      按巡检结果跳过 $flagged，按安全规则拒绝 $reject（计划 $planned 条）。"
    # 一个文件都没落地却打印「文件已保存到 …」，是在报告一件没发生的事。
    if (($ok + $skip + $unverified) -gt 0) { Write-Host "文件已保存到：$Out" }
    else { Write-Host '没有任何文件成功保存。' }
    Write-Host '__PRIMARY_ONLY__'
    Write-Host '本工具没有下载过这些文件，也没有在你的机器上测试过这个脚本。'
    Write-Host '详情见 logs\report.tsv。'
}
if ([Environment]::UserInteractive -and -not [Console]::IsOutputRedirected) { Read-Host '按回车键关闭' | Out-Null }
'''


def render_sh(plan: dict) -> str:
    return _SH.replace("__PRIMARY_ONLY__", primary_only_sentence(plan))


def render_ps1(plan: dict) -> str:
    return _PS1.replace("__PRIMARY_ONLY__", primary_only_sentence(plan))


def artifact_files(plan: dict, meta: dict) -> list[dict]:
    """脚本相关的四份文件。`newline` / `bom` 决定落盘时怎么编码——不是可选项。"""
    return [
        {"path": "manifest.tsv", "text": render_manifest_tsv(plan, meta), "newline": "\n", "bom": False},
        {"path": "manifest.json", "text": render_manifest_json(plan, meta), "newline": "\n", "bom": False},
        {"path": "file-list.md", "text": render_file_list_md(plan), "newline": "\n", "bom": False},
        {"path": "download.sh", "text": render_sh(plan), "newline": "\n", "bom": False, "executable": True},
        # Windows PowerShell 5.1 读无 BOM 的 UTF-8 会把中文显示成乱码，这里必须带 BOM。
        {"path": "download.ps1", "text": render_ps1(plan), "newline": "\r\n", "bom": True},
    ]
