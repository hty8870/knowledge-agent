# -*- coding: utf-8 -*-
"""对话式数据库管护的命令行入口（薄壳）：清点 / 本地导入 / 联网搜索 / 回收站删除 / 恢复。

实现全在 `src/dataset_recommender/corpus/corpus_curation.py`（与 MCP `curate_datasets`、Web
`/api/curate/*` 共用 `run_curate_action` 单一真源分发），这里只解析参数后转发。

默认是 plan（预览，零写盘；search_online 的 plan 会真实联网查官方源并记请求账本）；
加 `--apply --confirm-token <token>` 才真执行（写盘/联网）。

  py scripts/curate_datasets.py --action list
  py scripts/curate_datasets.py --action import --payload-file data.json --filename my.json
  py scripts/curate_datasets.py --action import --payload-file data.json --apply --confirm-token <token>
  py scripts/curate_datasets.py --action search_online --query "lung atlas" --species Human --limit 10
  py scripts/curate_datasets.py --action search_online --apply --confirm-token <token> --plan-file plan.json
  py scripts/curate_datasets.py --action remove --filename upload_20260801_xxxx_x.json
  py scripts/curate_datasets.py --action restore --filename 20260801_xxxx_x_upload_xxx.json --apply --confirm-token <token>

管护对象限 `database/external/` 的 `upload_*` 命名空间；`database/base/` 冻结基准结构性不可达。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="对话式数据库管护（plan 默认零写盘；--apply --confirm-token 才真执行）。",
    )
    p.add_argument("--action", required=True, choices=list(cc.ACTIONS), help="管护动作")
    p.add_argument("--query", default=None, help="search_online 的搜索关键词")
    p.add_argument("--source", default=None,
                   help="import 归属来源名 / search_online 官方源键（默认 arrayexpress；"
                        "可选 cellxgene / hubmap / single_cell_portal）")
    p.add_argument("--species", default=None, help="search_online 物种过滤（本地子串过滤）")
    p.add_argument("--limit", type=int, default=None, help="search_online 候选上限（1–100，默认 20）")
    p.add_argument("--filename", default=None,
                   help="import 落盘名 / remove 的 external 文件名 / restore 的回收站文件名")
    p.add_argument("--payload", default=None, help="import 的数据集 JSON 文本")
    p.add_argument("--payload-file", default=None, help="import 的数据集 JSON 文件路径（与 --payload 二选一）")
    p.add_argument("--plan-file", default=None,
                   help="search_online apply：保存有 plan 完整返回（含 candidates）的 JSON 文件")
    p.add_argument("--apply", action="store_true", help="真执行（默认是 plan 预览）；必须配 --confirm-token")
    p.add_argument("--confirm-token", default=None, help="plan 返回的 confirm_token")
    p.add_argument("--force", action="store_true", help="import 撞内容重复时仍确认入库")
    p.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    return p


def _summary_zh(result: dict) -> str:
    """人读摘要（一行一行拼；--json 时不用）。"""
    action = result.get("action", "")
    dry = result.get("dry_run", True)
    lines = [f"动作：{action}（{'预览 plan，未写盘' if dry else '已执行 apply'}）"]
    if action == "curate.list":
        lines.append(f"外部库文件 {result['file_count']} 个；回收站 {result['recycle_count']} 个。")
        for f in result.get("files", []):
            lines.append(f"  - {f['filename']}：{f.get('record_count')} 条，来源 {f.get('sources')}"
                         + ("" if f.get("curatable") else "（官方快照，不可对话式管护）"))
        for r in result.get("recycle", []):
            lines.append(f"  [回收站] {r['recycle_name']}（原 {r['original_filename']}，{r.get('record_count')} 条）")
    elif action == "curate.import":
        if dry:
            lines.append(f"将导入 {result['record_count']} 条 → {result['filename']}；来源 {result.get('sources')}")
            dup = result.get("duplicate") or {}
            if dup.get("is_duplicate"):
                lines.append(f"⚠️ 内容与既有文件整集重复：{'、'.join(dup.get('matched_files') or [])}（确认需 --force）")
        else:
            lines.append(f"已导入 {result['record_count']} 条 → {result['saved_to']}")
    elif action == "curate.search_online":
        if dry:
            lines.append(f"{result.get('source_label')} 查询 {result.get('query')!r}：{result['record_count']} 条候选（未落盘）")
            for t in result.get("sample_titles", []):
                lines.append(f"  - {t}")
        else:
            lines.append(f"已入库 {result['record_count']} 条 → {result['saved_to']}")
    elif action == "curate.remove":
        if dry:
            lines.append(f"将把 {result['filename']}（{result['record_count']} 条）移入回收站（可逆，非真删除）。")
        else:
            lines.append(f"已移入回收站：{result['moved_to']}（restore 可移回）")
    elif action == "curate.restore":
        if dry:
            lines.append(f"将把回收站 {result['recycle_name']} 移回 external/{result['target_filename']}"
                         + ("（⚠️ 目标名已存在，apply 会拒绝）" if result.get("will_conflict") else ""))
        else:
            lines.append(f"已移回：{result['restored_to']}")
    for w in result.get("warnings") or []:
        lines.append(f"提示：{w}")
    if result.get("confirm_token") and dry:
        lines.append(f"confirm_token：{result['confirm_token']}（确认时加 --apply --confirm-token 回传）")
    if result.get("write_boundary"):
        lines.append(f"边界：{result['write_boundary']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload_bytes: bytes | None = None
    if args.payload and args.payload_file:
        print("bad_param: --payload 与 --payload-file 二选一。", file=sys.stderr)
        return 1
    if args.payload_file:
        path = Path(args.payload_file)
        if not path.is_file():
            print(f"bad_param: --payload-file 指向的文件不存在：{args.payload_file}", file=sys.stderr)
            return 1
        payload_bytes = path.read_bytes()
    elif args.payload:
        payload_bytes = args.payload.encode("utf-8")
    plan_result: dict | None = None
    if args.plan_file:
        path = Path(args.plan_file)
        if not path.is_file():
            print(f"bad_param: --plan-file 指向的文件不存在：{args.plan_file}", file=sys.stderr)
            return 1
        try:
            plan_result = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"bad_param: --plan-file 不是合法 JSON：{exc}", file=sys.stderr)
            return 1
    try:
        result = cc.run_curate_action(
            args.action,
            dry_run=not args.apply,
            query=args.query,
            source=args.source,
            species=args.species,
            limit=args.limit,
            filename=args.filename,
            payload_bytes=payload_bytes,
            plan_result=plan_result,
            confirm_token=args.confirm_token,
            force=args.force,
            project_root=PROJECT_ROOT,
        )
    except cc.CurateError as exc:
        print(f"{exc.code}: {exc.hint}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    else:
        print(_summary_zh(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
