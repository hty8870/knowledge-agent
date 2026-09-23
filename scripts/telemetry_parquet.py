#!/usr/bin/env python3
"""Convert exported telemetry JSONL into lossless, analysis-ready Parquet.

This optional, offline-only tool deliberately has no database connection and
does not send data over the network.  It streams one input JSONL at a time,
writes to a new temporary sibling directory, then atomically publishes the
finished directory.  Existing outputs are never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

try:  # Kept optional so normal application installs do not pull in Arrow.
    import pyarrow as pa
    import pyarrow.parquet as pq
except ModuleNotFoundError:  # pragma: no cover - exercised without optional dependency
    pa = None
    pq = None


SCRIPT_VERSION = "1"
MANIFEST_SCHEMA = "biodata-telemetry-parquet/1"
DEFAULT_BATCH_SIZE = 10_000


class ParquetError(RuntimeError):
    """Safe, user-facing conversion failure."""


def _require_pyarrow() -> None:
    if pa is None or pq is None:
        raise ParquetError(
            "缺少可选依赖 pyarrow；请使用当前 Python 运行："
            "python -m pip install --require-hashes -r requirements/requirements-analytics.lock"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fixed_schema():
    _require_pyarrow()
    assert pa is not None
    # Scalar columns cover the common join/filter dimensions; row_json retains
    # the complete canonical source record without schema-loss as contracts grow.
    return pa.schema([
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("line_number", pa.int64(), nullable=False),
        pa.field("record_type", pa.string()),
        pa.field("received_at", pa.string()),
        pa.field("ts", pa.string()),
        pa.field("install_id", pa.string()),
        pa.field("packet_id", pa.string()),
        pa.field("record_id", pa.string()),
        pa.field("tid", pa.string()),
        pa.field("iid", pa.string()),
        pa.field("sid", pa.string()),
        pa.field("event_id", pa.string()),
        pa.field("call_id", pa.string()),
        pa.field("feedback_id", pa.string()),
        pa.field("policy", pa.string()),
        pa.field("policy_id_str", pa.string()),
        pa.field("route", pa.string()),
        pa.field("prompt_version", pa.string()),
        pa.field("experiment_id", pa.string()),
        pa.field("experiment_arm", pa.string()),
        pa.field("propensity", pa.float64()),
        pa.field("training_consent", pa.bool_()),
        pa.field("contract_version", pa.int32()),
        pa.field("row_json", pa.string(), nullable=False),
    ])


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _flat_row(record: dict[str, Any], *, source_name: str, line_number: int) -> dict[str, Any]:
    return {
        "source_file": source_name,
        "line_number": line_number,
        "record_type": _text(record.get("record_type") or record.get("kind")),
        "received_at": _text(record.get("received_at")),
        "ts": _text(record.get("ts") if "ts" in record else record.get("t")),
        "install_id": _text(record.get("install_id")),
        "packet_id": _text(record.get("packet_id")),
        "record_id": _text(record.get("record_id")),
        "tid": _text(record.get("tid")),
        "iid": _text(record.get("iid")),
        "sid": _text(record.get("sid")),
        "event_id": _text(record.get("event_id")),
        "call_id": _text(record.get("call_id")),
        "feedback_id": _text(record.get("feedback_id")),
        "policy": _text(record.get("policy")),
        "policy_id_str": _text(record.get("policy_id_str")),
        "route": _text(record.get("route")),
        "prompt_version": _text(record.get("prompt_version")),
        "experiment_id": _text(record.get("experiment_id")),
        "experiment_arm": _text(record.get("experiment_arm")),
        "propensity": _number(record.get("propensity")),
        "training_consent": record.get("training_consent") if isinstance(record.get("training_consent"), bool) else None,
        "contract_version": _integer(record.get("contract_version")),
        "row_json": _canonical_json(record),
    }


def _iter_jsonl(path: Path, *, source_name: str) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ParquetError(f"JSONL 损坏：{path.name}:{line_number}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ParquetError(f"JSONL 行必须是对象：{path.name}:{line_number}")
            yield _flat_row(record, source_name=source_name, line_number=line_number)


def _chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _schema_manifest(schema: Any) -> list[dict[str, Any]]:
    return [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema]


def _validate_output(inputs: list[Path], output: Path) -> tuple[list[Path], Path]:
    if not inputs:
        raise ParquetError("至少需要一个 --input JSONL 文件")
    resolved_inputs: list[Path] = []
    for item in inputs:
        resolved = item.resolve()
        if not resolved.is_file() or resolved.suffix.casefold() != ".jsonl":
            raise ParquetError(f"输入必须是存在的 .jsonl 文件：{item}")
        resolved_inputs.append(resolved)
    target = output.resolve()
    parts = [part.casefold() for part in target.parts]
    if any(parts[index:index + 2] == ["database", "base"] for index in range(len(parts) - 1)):
        raise ParquetError("安全拒绝：输出路径不得位于 database/base 冻结基准中")
    if target.exists():
        raise ParquetError(f"输出目录已存在，拒绝覆盖：{target}")
    # A JSONL source is a file, so a fresh directory can safely be a sibling or
    # child of its export directory.  The strict non-existent-target rule still
    # prevents overwriting raw input or a previous materialization.
    if any(target == source for source in resolved_inputs):
        raise ParquetError("输出目录不能与输入文件相同")
    return resolved_inputs, target


def _unique_names(inputs: list[Path]) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for index, path in enumerate(inputs, start=1):
        candidate = f"{path.stem}.parquet"
        if candidate.casefold() in used:
            candidate = f"{path.stem}-{index}.parquet"
        used.add(candidate.casefold())
        names.append(candidate)
    return names


def _replace_dir_with_retry(temporary: Path, target: Path) -> None:
    """原子发布输出目录；Windows 上索引器/杀软可能短暂持有新建目录句柄，对 PermissionError 做有界退避重试。"""
    delay = 0.05
    for attempt in range(10):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def convert(inputs: list[Path], output: Path, *, batch_size: int = DEFAULT_BATCH_SIZE) -> Path:
    """Create one Parquet file per JSONL input and atomically publish *output*."""
    _require_pyarrow()
    if batch_size <= 0:
        raise ParquetError("batch_size 必须为正整数")
    resolved_inputs, target = _validate_output(inputs, output)
    assert pa is not None and pq is not None
    schema = _fixed_schema()
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    if temporary.exists():  # astronomically unlikely, but never overwrite a foreign directory.
        raise ParquetError(f"临时输出目录已存在，拒绝覆盖：{temporary}")
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        file_entries: list[dict[str, Any]] = []
        for source, parquet_name in zip(resolved_inputs, _unique_names(resolved_inputs)):
            parquet_path = temporary / parquet_name
            rows = 0
            with pq.ParquetWriter(parquet_path, schema, compression="zstd") as writer:
                for batch in _chunks(_iter_jsonl(source, source_name=source.name), batch_size):
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    rows += len(batch)
            file_entries.append({
                "input": {"path": str(source), "sha256": _sha256_file(source)},
                "output": {"path": parquet_name, "sha256": _sha256_file(parquet_path)},
                "rows": rows,
            })
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "script_version": SCRIPT_VERSION,
            "batch_size": batch_size,
            "storage": {"format": "parquet", "compression": "zstd"},
            "parquet_schema": _schema_manifest(schema),
            "files": file_entries,
        }
        (temporary / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
        _replace_dir_with_retry(temporary, target)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将遥测 JSONL 离线流式转换为无损 Parquet 分析层")
    parser.add_argument("--input", action="append", required=True, type=Path,
                        help="输入 JSONL；可重复指定，每个输入生成一个 Parquet")
    parser.add_argument("--out", required=True, type=Path, help="此前不存在的输出目录")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)
    try:
        target = convert(args.input, args.out, batch_size=args.batch_size)
    except (ParquetError, OSError) as exc:
        # Never include records, request headers, or credentials in CLI errors.
        print(f"telemetry parquet failed: {exc}", file=sys.stderr)
        return 2
    print(f"telemetry parquet complete: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
