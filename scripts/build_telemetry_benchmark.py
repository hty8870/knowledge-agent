#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结遥测 benchmark 候选为可复现、不可变的数据集目录。

输入必须是 ``telemetry_export.py`` 产生的 ``benchmark_candidates.jsonl``。本工具只读输入，
只会新建一个此前不存在的输出目录；拒绝任何名为 database/base 的输出路径，绝不修改项目数据
库或基础数据。训练用途严格要求人工标注和 ``training_consent: true``；评测用途可用
``--purpose evaluation`` 显式纳入未授权训练的数据，但 manifest 会如实记录用途与授权统计。

不依赖第三方库。为保证同一用户、同一确定性查询语义簇、同一 UTC 时间桶绝不跨 split，三类
group key 会先形成连通组件，再整体分配到 train/validation/test。小样本允许某些 split 为空，
但绝不会为了填满 split 打破防泄漏约束。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_VERSION = "telemetry-benchmark-freeze/1"
SPLITS = ("train", "validation", "test")
DEFAULT_RATIOS = (0.8, 0.1, 0.1)


class BenchmarkError(ValueError):
    """输入或输出契约不满足时的 fail-closed 错误。"""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_created_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BenchmarkError("--created-at 必须是 ISO-8601 UTC 时间") from exc
    if parsed.tzinfo is None:
        raise BenchmarkError("--created-at 必须带时区，建议以 Z 结尾")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        # 遥测客户端的 t/ts 是 epoch milliseconds；小的秒级值也兼容。
        seconds = float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _normalise_query(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _query_tokens(normalised: str) -> tuple[str, ...]:
    """纯标准库的稳定词元：英文/数字词和逐字中文都保留，词序不影响簇。"""
    tokens = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalised)
    return tuple(sorted(set(tokens)))


def _semantic_cluster_id(normalised_query: str) -> str:
    """确定性 lexical-semantic 近似：同一词元集合（含中英文规范化）必在同一簇。"""
    tokens = _query_tokens(normalised_query)
    material = "tokens:" + "\u001f".join(tokens) if tokens else "query:" + normalised_query
    return "qcl-" + _sha256_bytes(material.encode("utf-8"))[:16]


def _normalise_topk(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in value:
        uid = str(raw or "").strip()
        if uid and uid not in seen:
            out.append(uid)
            seen.add(uid)
    return out


def _is_human_labeled(rating: Any) -> bool:
    if not isinstance(rating, dict):
        return False
    return any(rating.get(key) not in (None, "", [])
               for key in ("stars", "completion", "reasons", "useful_uids", "useful_idx", "comment"))


def _relevance_overrides(record: dict[str, Any], rating: dict[str, Any]) -> dict[str, int]:
    raw = record.get("relevance_grades", rating.get("relevance_grades"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BenchmarkError("relevance_grades 必须是 {dataset_uid: 非负整数} 对象")
    out: dict[str, int] = {}
    for raw_uid, raw_grade in raw.items():
        uid = str(raw_uid or "").strip()
        if not uid or isinstance(raw_grade, bool) or not isinstance(raw_grade, int) or raw_grade < 0:
            raise BenchmarkError("relevance_grades 必须是非空 uid 到非负整数的映射")
        out[uid] = raw_grade
    return out


def _time_bucket(timestamp: datetime, days: int) -> str:
    ordinal = timestamp.date().toordinal()
    start = ordinal - (ordinal % days)
    # date.fromordinal 的结果是 UTC 桶起点；字符串就是稳定分组 key。
    return date.fromordinal(start).isoformat()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BenchmarkError(f"无法读取输入 JSONL：{path}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"输入第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise BenchmarkError(f"输入第 {line_number} 行必须是 JSON 对象")
        records.append(value)
    return records


def _prepare_records(records: list[dict[str, Any]], *, purpose: str, bucket_days: int) -> tuple[list[dict[str, Any]], Counter[str]]:
    exclusions: Counter[str] = Counter()
    prepared: list[dict[str, Any]] = []
    for record in records:
        rating = record.get("rating")
        if not _is_human_labeled(rating):
            exclusions["no_human_label"] += 1
            continue
        if purpose == "training" and record.get("training_consent") is not True:
            exclusions["no_training_consent"] += 1
            continue
        install_id = str(record.get("install_id") or "").strip()
        if not install_id:
            exclusions["missing_install_id"] += 1
            continue
        query = _normalise_query(record.get("query"))
        if not query:
            exclusions["missing_query"] += 1
            continue
        topk = _normalise_topk(record.get("system_topk_uids"))
        if not topk:
            exclusions["empty_topk"] += 1
            continue
        timestamp = _parse_timestamp(record.get("ts"))
        if timestamp is None:
            exclusions["invalid_timestamp"] += 1
            continue
        assert isinstance(rating, dict)  # _is_human_labeled 已保证
        overrides = _relevance_overrides(record, rating)
        useful = {str(uid).strip() for uid in (rating.get("useful_uids") or []) if str(uid).strip()}
        graded = [{"uid": uid, "grade": overrides.get(uid, 2 if uid in useful else 0)} for uid in topk]
        dedupe_key = _sha256_bytes(_canonical_json({"query": query, "topk": topk}).encode("utf-8"))
        source_id = _canonical_json({
            "record_id": record.get("record_id"), "packet_id": record.get("packet_id"),
            "install_id": install_id, "ts": record.get("ts"), "query": query, "topk": topk,
        })
        prepared.append({
            "example_id": "tbx-" + _sha256_bytes(source_id.encode("utf-8"))[:20],
            "record_id": record.get("record_id"), "packet_id": record.get("packet_id"),
            "install_id": install_id,
            "ts": record.get("ts"),
            "query": record.get("query"),
            "query_normalized": query,
            "semantic_cluster": _semantic_cluster_id(query),
            "time_bucket_utc": _time_bucket(timestamp, bucket_days),
            "system_topk_uids": topk,
            "graded_relevance": graded,
            "rating": rating,
            "policy": record.get("policy"),
            "experiment_arm": record.get("experiment_arm"),
            "propensity": record.get("propensity"),
            "prompt_version": record.get("prompt_version"),
            "route": record.get("route"),
            "training_consent": record.get("training_consent") is True,
            "_dedupe_key": dedupe_key,
            "_source_sort": _sha256_bytes(_canonical_json(record).encode("utf-8")),
        })

    # Dedupe is independent of input order: retain the canonical-lowest full source representation.
    deduped: dict[str, dict[str, Any]] = {}
    for row in prepared:
        previous = deduped.get(row["_dedupe_key"])
        if previous is None or (row["_source_sort"], row["example_id"]) < (previous["_source_sort"], previous["example_id"]):
            if previous is not None:
                exclusions["duplicate_query_topk"] += 1
            deduped[row["_dedupe_key"]] = row
        else:
            exclusions["duplicate_query_topk"] += 1
    return sorted(deduped.values(), key=lambda row: (row["_dedupe_key"], row["_source_sort"])), exclusions


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[max(left, right)] = min(left, right)


def _components(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    dsu = _DisjointSet(len(rows))
    first_by_group: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for kind, value in (("install", row["install_id"]), ("cluster", row["semantic_cluster"]),
                            ("bucket", row["time_bucket_utc"])):
            key = (kind, str(value))
            other = first_by_group.setdefault(key, index)
            dsu.union(index, other)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[dsu.find(index)].append(row)
    return sorted(grouped.values(), key=lambda component: _component_key(component))


def _component_key(component: list[dict[str, Any]]) -> str:
    values = sorted(row["example_id"] for row in component)
    return _sha256_bytes("\n".join(values).encode("utf-8"))


def _split_components(rows: list[dict[str, Any]], ratios: tuple[float, float, float]) -> dict[str, list[dict[str, Any]]]:
    components = _components(rows)
    assigned: dict[str, list[dict[str, Any]]] = {split: [] for split in SPLITS}
    counts = {split: 0 for split in SPLITS}
    total = len(rows)
    targets = {split: ratio * total for split, ratio in zip(SPLITS, ratios)}
    for component in components:
        # First choose the split with largest outstanding target; stable name order resolves ties.
        split = max(SPLITS, key=lambda name: (targets[name] - counts[name], -SPLITS.index(name)))
        assigned[split].extend(component)
        counts[split] += len(component)
    for split in SPLITS:
        assigned[split].sort(key=lambda row: row["example_id"])
    return assigned


def _safe_target(out_root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise BenchmarkError("run_id 只能包含字母、数字、.、_、-，且不能含路径分隔符")
    root = out_root.resolve()
    target = (root / run_id).resolve()
    if target.parent != root:
        raise BenchmarkError("输出目标必须是 --out-root 的直接子目录")
    forbidden = {"database", "base"}
    if any(part.casefold() in forbidden for part in target.parts):
        raise BenchmarkError("安全拒绝：冻结 benchmark 不允许写入 database/base 路径")
    if target.exists():
        raise BenchmarkError(f"冻结目录已存在，拒绝覆盖：{target}")
    return target


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            public = {key: value for key, value in row.items() if not key.startswith("_")}
            handle.write(_canonical_json(public) + "\n")


def build_benchmark(input_path: Path, out_root: Path, *, run_id: str | None, purpose: str,
                    created_at: str | None, time_bucket_days: int, ratios: tuple[float, float, float]) -> Path:
    if purpose not in {"training", "evaluation"}:
        raise BenchmarkError("purpose 必须是 training 或 evaluation")
    if time_bucket_days <= 0:
        raise BenchmarkError("--time-bucket-days 必须为正整数")
    if any(ratio < 0 for ratio in ratios) or sum(ratios) <= 0:
        raise BenchmarkError("split 比例必须非负且总和大于零")
    ratio_total = sum(ratios)
    ratios = tuple(ratio / ratio_total for ratio in ratios)
    raw_input = input_path.read_bytes()
    records = _read_jsonl(input_path)
    rows, exclusions = _prepare_records(records, purpose=purpose, bucket_days=time_bucket_days)
    created = _parse_created_at(created_at)
    input_hash = _sha256_bytes(raw_input)
    frozen_run_id = run_id or f"telemetry-{input_hash[:16]}"
    target = _safe_target(out_root, frozen_run_id)
    splits = _split_components(rows, ratios)

    # All validation happened before creating the directory: malformed input never leaves a partial artifact.
    target.mkdir(parents=True, exist_ok=False)
    file_hashes: dict[str, str] = {}
    split_stats: dict[str, dict[str, int]] = {}
    for split in SPLITS:
        filename = f"{split}.jsonl"
        path = target / filename
        _write_jsonl(path, splits[split])
        file_hashes[filename] = _sha256_file(path)
        split_stats[split] = {
            "rows": len(splits[split]),
            "install_ids": len({row["install_id"] for row in splits[split]}),
            "semantic_clusters": len({row["semantic_cluster"] for row in splits[split]}),
            "time_buckets_utc": len({row["time_bucket_utc"] for row in splits[split]}),
        }

    manifest = {
        "schema": "biodata-telemetry-benchmark/1",
        "script_version": SCRIPT_VERSION,
        "run_id": frozen_run_id,
        "created_at": created,
        "purpose": purpose,
        "authorization": {
            "training_consent_required": purpose == "training",
            "evaluation_may_include_without_training_consent": purpose == "evaluation",
        },
        "input": {"path": str(input_path), "sha256": input_hash, "records": len(records)},
        "parameters": {
            "time_bucket_days": time_bucket_days,
            "split_ratios": {split: ratio for split, ratio in zip(SPLITS, ratios)},
            "dedupe": "normalized_query_plus_ordered_unique_topk",
            "semantic_cluster": "deterministic_normalized_token_set_v1",
            "leakage_groups": ["install_id", "semantic_cluster", "time_bucket_utc"],
        },
        "rows_after_filters": len(rows),
        "exclusions": dict(sorted(exclusions.items())),
        "splits": split_stats,
        "files": {name: {"sha256": digest} for name, digest in sorted(file_hashes.items())},
    }
    (target / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return target


def _parse_ratios(value: str) -> tuple[float, float, float]:
    try:
        ratios = tuple(float(item.strip()) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--split-ratios 形如 0.8,0.1,0.1") from exc
    if len(ratios) != 3:
        raise argparse.ArgumentTypeError("--split-ratios 必须恰有 train,validation,test 三个数")
    return ratios  # further validation in build_benchmark gives one consistent error path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结 telemetry benchmark candidates 为不可变 split 数据集")
    parser.add_argument("--input", required=True, type=Path, help="telemetry_export.py 的 benchmark_candidates.jsonl")
    parser.add_argument("--out-root", required=True, type=Path, help="冻结运行目录的父目录")
    parser.add_argument("--run-id", help="可选、稳定的运行名；省略时由输入 SHA-256 派生")
    parser.add_argument("--purpose", choices=("training", "evaluation"), default="training")
    parser.add_argument("--created-at", help="可注入 ISO-8601 UTC 时间，使 manifest 可复现")
    parser.add_argument("--time-bucket-days", type=int, default=1)
    parser.add_argument("--split-ratios", type=_parse_ratios, default=DEFAULT_RATIOS)
    args = parser.parse_args(argv)
    try:
        target = build_benchmark(args.input, args.out_root, run_id=args.run_id, purpose=args.purpose,
                                 created_at=args.created_at, time_bucket_days=args.time_bucket_days,
                                 ratios=args.split_ratios)
    except (BenchmarkError, OSError) as exc:
        print(f"benchmark freeze failed: {exc}", file=sys.stderr)
        return 2
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
