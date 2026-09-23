# -*- coding: utf-8 -*-
"""upload 运行时产物的**晋升机制**——把 curate sync 自动入库的 `upload_*.json`
从 `database/external/`（运行时产物，归属基础来源）晋升进 tracked 基础快照
`database/base/10x-synced.json`。

为什么（治本）：sync 自动入库的记录住在外部目录、归属 BASE_SOURCE，而外部目录只在
「选择集含至少一个外部来源」时才装载（corpus.load_normalized_corpus 的既有结构）——
面板计数（base+外部全量）784 与 [10x] 单选 774 之间由此产生 UX 缝隙。晋升后这批记录
成为基础语料本体：None==[10x]==面板 自动一致（None 是冻结评测口径——只装 base，
而晋升目标正是 base，评测口径语义不变）。

纪律（与 sync 写入同口径）：
- **机械闸**（任一不过 → 该文件整批不晋升、如实报告、CLI 退出码非零）：
  包裹三键齐全（source/record_count/records）；record_count==len(records)；
  每条有非空 dataset_uid/url/dataset_name；每条 source==BASE_SOURCE；
  逐条与 base 现存去重——uid 精确 > url 精确 > name 精确，直接复用
  `corpus.locate_record`（唯一定位真源），不自写去重逻辑。
- **不改写 `10x-Visium.json`**（策展产物保持原样）：晋升落独立的 `10x-synced.json`，
  同步批次独立可回滚（删该文件 + 归档搬回即还原）。
- **强制流水账**：每次成功晋升写一行 `.userdata/uploads_journal.jsonl`
  （action="promote"，与 ingest 同一份账同一个追加函数）；账写不进去 → 回滚 base 写出。
- 归档即留痕：原 upload 文件搬入 `research/promotions/`（tracked，
  流水线目录整体迁出 database/ 后的新落点），
  同名加 `.promoted` 后缀，旁边落 `.manifest.json`（时间/来源文件/条数/uid 清单/指纹）。
- 纯标准库 + 项目内 import；确定性（文件名排序处理、uid 排序写出）。

用法：
    py -3 scripts/promote_uploads.py                 # 扫描晋升（真实仓库根）
    py -3 scripts/promote_uploads.py --project-root X
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.corpus.corpus import (  # noqa: E402
    BASE_SOURCE,
    EXTERNAL_DIR_NAME,
    invalidate_base_cache,
    invalidate_external_cache,
    locate_record,
)
from dataset_recommender.corpus.data_loader import RawRecord, load_raw_records  # noqa: E402
from dataset_recommender.corpus.uploads import _append_upload_journal  # noqa: E402
from dataset_recommender.retrieval.normalizer import normalize_records  # noqa: E402

#: 晋升目标文件（database/base/ 内；与策展产物 10x-Visium.json 并列、独立可回滚）。
BASE_SYNCED_NAME = "10x-synced.json"
#: 归档目录（顶层 research/promotions，tracked：归档即留痕；
#: 自旧 database 下 workstream 落点迁出）。
PROMOTIONS_REL = Path("research") / "promotions"
#: 包裹必备三键（缺即拒——形状不符的 upload 不猜不修）。
_WRAPPER_KEYS = ("source", "record_count", "records")
#: 每条记录的身份三键（缺一即拒——定位/去重/上屏都靠它们）。
_IDENTITY_KEYS = ("dataset_uid", "url", "dataset_name")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gate_payload(
    payload: Any,
    gate_corpus: list,
    *,
    filename: str,
) -> str:
    """机械闸（单文件，整批语义）。返回 ""=通过；否则人读原因（该文件整批不晋升）。

    `gate_corpus` 是 base 现存（含既有 10x-synced.json）+ 本批已接受文件的记录
    （DatasetRecord 列表）——跨文件批内撞重也按重复拒。"""
    if not isinstance(payload, dict):
        return "不是包裹对象（{source, record_count, records}）"
    for key in _WRAPPER_KEYS:
        if key not in payload:
            return f"包裹缺键 {key!r}"
    records = payload["records"]
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        return "records 不是记录对象数组"
    try:
        declared = int(payload["record_count"])
    except (TypeError, ValueError):
        return f"record_count 不是整数（{payload['record_count']!r}）"
    if declared != len(records):
        return f"record_count={declared} 与 records 实际 {len(records)} 条不符"
    for i, rec in enumerate(records, 1):
        for key in _IDENTITY_KEYS:
            if not str(rec.get(key) or "").strip():
                return f"第 {i} 条缺身份键 {key!r}（{filename}）"
        src = str(rec.get("source") or "").strip()
        if src != BASE_SOURCE:
            return f"第 {i} 条 source={src!r}，不是 {BASE_SOURCE!r}（{filename}）"
        hit, candidates = locate_record(
            gate_corpus,
            uid=str(rec.get("dataset_uid") or ""),
            url=str(rec.get("url") or ""),
            name=str(rec.get("dataset_name") or ""),
        )
        if hit is not None or candidates:
            return (f"第 {i} 条与 base 现存重复（uid={rec.get('dataset_uid')!r}，"
                    f"uid 精确 > url 精确 > name 精确，locate_record 同序；{filename}）")
    return ""


def _journal_promote(project_root: Path, *, base_filename: str, record_count: int,
                     sha256: str, note: str) -> None:
    """晋升流水账（与 ingest 同一份账同一个追加函数；action="promote" 区分来路）。"""
    _append_upload_journal(project_root, {
        "ts": _utc_now_iso(),
        "action": "promote",
        "filename": base_filename,
        "record_count": record_count,
        "sha256": sha256,
        "note": note,
        "form_source": "promote_uploads",
    })


def promote_uploads(
    project_root: Path,
    *,
    log: Callable[[str], None] = print,
) -> dict:
    """扫描晋升主流程（可 import）。返回报告 dict；`report["ok"]`  False = 有拒绝/错误。

    批次语义：逐文件闸（整批通过/整批拒绝）；全部可晋升文件合并成**一次** base 写出
    （uid 排序、按 uid 内部去重——保留先到者），随后逐文件归档 + 逐文件流水账。
    流水账写不进去 → 回滚 base 写出（恢复原内容/删除新文件），归档不动，如实报错。
    """
    project_root = Path(project_root)
    ext_dir = project_root / EXTERNAL_DIR_NAME
    base_dir = project_root / "database" / "base"
    synced_path = base_dir / BASE_SYNCED_NAME
    report: dict[str, Any] = {
        "ok": True, "promoted": [], "skipped": [], "rejected": [],
        "base_file": str(synced_path.relative_to(project_root).as_posix())
        if synced_path.is_relative_to(project_root) else synced_path.name,
        "promoted_total": 0,
    }

    files = sorted(p for p in ext_dir.glob("upload_*.json") if p.is_file()) \
        if ext_dir.is_dir() else []
    if not files:
        log("没有待晋升的 upload_*.json（空晋升，幂等）。")
        return report

    # 闸用语料：base 现存（含既有 10x-synced.json），与 _load_base 同径（无缓存直读）。
    gate_corpus = normalize_records(load_raw_records(base_dir))

    accepted: list[tuple[Path, bytes, list[dict]]] = []
    for path in files:
        raw_bytes = path.read_bytes()
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            report["rejected"].append({"file": path.name, "reason": f"非法 JSON：{exc}"})
            continue
        wrapper_source = str(payload.get("source") or "").strip() \
            if isinstance(payload, dict) else ""
        if wrapper_source != BASE_SOURCE:
            # 外源 upload（当前 sync 只做 10x，外源是将来事）：跳过并报告，不算失败。
            report["skipped"].append({
                "file": path.name,
                "reason": f"来源 {wrapper_source or '（未声明）'!r} 非 {BASE_SOURCE!r}，外源晋升尚未支持",
            })
            continue
        reason = _gate_payload(payload, gate_corpus, filename=path.name)
        if reason:
            report["rejected"].append({"file": path.name, "reason": reason})
            continue
        accepted.append((path, raw_bytes, list(payload["records"])))
        # 已接受记录并入闸用语料：本批后续文件与它撞重也按重复拒（locate_record 同径）。
        gate_corpus = gate_corpus + normalize_records(
            [RawRecord(source_file=path.name, record=r) for r in payload["records"]])

    for item in report["skipped"]:
        log(f"跳过（外源）：{item['file']}——{item['reason']}")
    for item in report["rejected"]:
        log(f"拒绝（整批不晋升）：{item['file']}——{item['reason']}")
    if report["rejected"]:
        report["ok"] = False
    if not accepted:
        if not report["skipped"] and not report["rejected"]:
            log("没有待晋升的 upload_*.json（空晋升，幂等）。")
        return report

    # 合并写出（一次）：既有 10x-synced.json + 本批全部新记录；按 uid 内部去重
    # （保留先到者——闸已保证与 base 不撞，这里兜同批/旧文件的 uid 撞单），uid 排序。
    existing_synced: list[dict] = []
    old_synced_bytes: bytes | None = None
    if synced_path.exists():
        old_synced_bytes = synced_path.read_bytes()
        try:
            old_payload = json.loads(old_synced_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            report["ok"] = False
            report["rejected"].append({
                "file": BASE_SYNCED_NAME,
                "reason": f"既有晋升文件不是合法 JSON（{exc}）——拒绝覆盖，请先人工核查",
            })
            log(f"错误：{BASE_SYNCED_NAME} 已存在但读不出（{exc}），本次未写任何内容。")
            return report
        if not isinstance(old_payload, list):
            report["ok"] = False
            report["rejected"].append({
                "file": BASE_SYNCED_NAME,
                "reason": "既有晋升文件不是记录数组——拒绝覆盖，请先人工核查",
            })
            log(f"错误：{BASE_SYNCED_NAME} 不是记录数组，本次未写任何内容。")
            return report
        existing_synced = old_payload

    merged: list[dict] = []
    seen_uids: set[str] = set()
    for rec in existing_synced:
        uid = str(rec.get("dataset_uid") or "")
        if uid and uid in seen_uids:
            continue
        seen_uids.add(uid)
        merged.append(rec)
    per_file_new: dict[str, list[str]] = {}
    for path, _raw, records in accepted:
        for rec in records:
            uid = str(rec.get("dataset_uid") or "")
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            merged.append(rec)
            per_file_new.setdefault(path.name, []).append(uid)
    merged.sort(key=lambda r: str(r.get("dataset_uid") or ""))

    new_total = sum(len(v) for v in per_file_new.values())
    base_dir.mkdir(parents=True, exist_ok=True)
    new_bytes = json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")
    synced_path.write_bytes(new_bytes)
    invalidate_base_cache()      # API 复用时生效（指纹键控缓存常规自失效，这里双保险）

    # 归档 + 流水账（逐文件）。账写不进去 → 回滚 base 写出，归档未动，如实报错。
    archive_dir = project_root / PROMOTIONS_REL
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path, raw_bytes, records in accepted:
        uids = per_file_new.get(path.name, [])
        note = (f"晋升：{path.name} → {BASE_SYNCED_NAME}"
                f"（{len(uids)}/{len(records)} 条；归档 {PROMOTIONS_REL.as_posix()}/）")
        try:
            _journal_promote(
                project_root, base_filename=BASE_SYNCED_NAME,
                record_count=len(uids), sha256=_sha256(raw_bytes), note=note)
        except OSError as exc:
            # 强制流水账纪律（同 ingest 的 journal_failed）：账在写在，账断回滚。
            if old_synced_bytes is None:
                synced_path.unlink(missing_ok=True)
            else:
                synced_path.write_bytes(old_synced_bytes)
            invalidate_base_cache()
            report["ok"] = False
            report["rejected"].append({
                "file": path.name,
                "reason": f"晋升流水账写不进去（{exc}）——base 写出已回滚，没有任何晋升生效",
            })
            log(f"错误：流水账写不进去（{exc}），base 已回滚，本次晋升未生效。")
            return report

        archived_as = archive_dir / f"{path.name}.promoted"
        archived_as.write_bytes(raw_bytes)
        path.unlink()
        manifest = {
            "ts": _utc_now_iso(),
            "action": "promote",
            "source_file": f"{EXTERNAL_DIR_NAME}/{path.name}",
            "archived_as": f"{PROMOTIONS_REL.as_posix()}/{archived_as.name}",
            "base_file": f"database/base/{BASE_SYNCED_NAME}",
            "promoted_count": len(uids),
            "skipped_count": len(records) - len(uids),
            "uids": uids,
            "sha256_source": _sha256(raw_bytes),
            "sha256_base_after": _sha256(synced_path.read_bytes()),
        }
        (archive_dir / f"{path.name}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        report["promoted"].append({
            "file": path.name, "count": len(uids), "uids": uids,
            "archived_as": manifest["archived_as"],
        })
        log(f"晋升：{path.name} → {BASE_SYNCED_NAME}（{len(uids)} 条）；"
            f"归档 {manifest['archived_as']} + manifest")

    invalidate_external_cache()  # upload 文件已搬走，外部库视图同步失效
    report["promoted_total"] = new_total
    log(f"完成：晋升 {new_total} 条 → {BASE_SYNCED_NAME}（写出共 {len(merged)} 条，uid 排序）。")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 curate sync 的 upload_*.json 晋升进 database/base/10x-synced.json")
    parser.add_argument("--project-root", default=str(ROOT), help="仓库根（默认脚本所在仓库）")
    args = parser.parse_args(argv)
    report = promote_uploads(Path(args.project_root))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
