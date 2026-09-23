from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PREFERRED_LIST_KEYS = ("records", "datasets", "data", "items", "results")


@dataclass(slots=True)
class RawRecord:
    source_file: str
    record: dict[str, Any]


def scan_json_files(data_dir: Path) -> list[Path]:
    """Return all JSON files directly under data_dir."""
    if not data_dir.exists():
        return []
    return sorted(path for path in data_dir.glob("*.json") if path.is_file())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _looks_like_record(obj: dict[str, Any]) -> bool:
    signal_keys = {
        "dataset_name",
        "title",
        "name",
        "species",
        "tissue",
        "disease",
        "chemistry",
        "technology",
        "assay",
        "url",
        "download_url",
    }
    return len(signal_keys.intersection({k.lower() for k in obj.keys()})) >= 2


def _collect_dict_lists(payload: Any, depth: int = 0, max_depth: int = 6) -> list[list[dict[str, Any]]]:
    if depth > max_depth:
        return []

    results: list[list[dict[str, Any]]] = []
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            results.append(payload)
        for item in payload:
            results.extend(_collect_dict_lists(item, depth + 1, max_depth=max_depth))
    elif isinstance(payload, dict):
        for key in PREFERRED_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                results.extend(_collect_dict_lists(value, depth + 1, max_depth=max_depth))
        for value in payload.values():
            results.extend(_collect_dict_lists(value, depth + 1, max_depth=max_depth))

    return results


def extract_records(payload: Any, _depth: int = 0, _max_depth: int = 64) -> list[dict[str, Any]]:
    """
    Extract dataset records from unknown JSON top-level structures.

    Rules:
    - list[dict] -> treat as records directly.
    - dict with records/datasets/data/items/results -> first preferred non-empty list.
    - otherwise recursively find all list[dict] and use the largest candidate.
    - if dict itself looks like a single dataset record, return [dict].

    深度有界（_max_depth，与 _collect_dict_lists 一致）：合法但深度嵌套的 JSON 降级为空结果，
    绝不触发 RecursionError（否则会逃出 load_raw_records 的 except、崩掉整目录装载）。
    """
    if _depth > _max_depth:
        return []
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            return payload
        merged: list[dict[str, Any]] = []
        for item in payload:
            merged.extend(extract_records(item, _depth + 1, _max_depth))
        return merged

    if isinstance(payload, dict):
        for key in PREFERRED_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                direct = extract_records(value, _depth + 1, _max_depth)
                if direct:
                    return direct

        if _looks_like_record(payload):
            return [payload]

        candidates = _collect_dict_lists(payload)
        if not candidates:
            return []
        largest = max(candidates, key=len)
        return [item for item in largest if isinstance(item, dict)]

    return []


def load_raw_records(data_dir: Path, *, lenient: bool = False) -> list[RawRecord]:
    """装载目录下所有 .json 的记录。

    - `lenient=False`（默认，**严格**）：任一文件解析失败即向上抛出。用于**基准语料 `database/base/`**——
      冻结的 767 条评测基准绝不能因某文件损坏被静默跳过、悄悄变少（否则「确定性」会在无人察觉下漂移）。
    - `lenient=True`（**宽容**）：单个 `.json` 损坏 → 打一行 stderr 并跳过，不连累同目录其它库。
      用于**外部库 `database/external/`**——用户可能手动放入坏文件，一个坏文件不该拖垮整库装载。
    """
    files = scan_json_files(data_dir)
    output: list[RawRecord] = []
    for file_path in files:
        try:
            payload = load_json(file_path)
            records = extract_records(payload)
        except (ValueError, OSError) as exc:
            if not lenient:
                raise
            print(
                f"[data_loader] 跳过无法解析的文件 {file_path.name}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        output.extend(RawRecord(source_file=file_path.name, record=record) for record in records)
    return output

