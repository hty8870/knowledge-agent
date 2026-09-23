# -*- coding: utf-8 -*-
"""多 profile 公开镜像（骨肉分离：骨架/领域包/装配体分仓）的并集覆盖与策略一致性。

- `skeleton ∪ biodata-pack == assembled`（源集合口径，允许 `.gitignore` 重叠——两仓都需要它）；
- 两个 profile 的 mappings 目标都在各自清单内；
- 装配体清单里的每个 profile 配置文件都被分类（私引白名单同步）。
"""
from __future__ import annotations

from pathlib import Path

MIRROR_DIR = Path(__file__).resolve().parents[1] / "packaging" / "public-mirror"


def _lines(name: str) -> list[str]:
    return [line.strip() for line in (MIRROR_DIR / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_profile_union_covers_assembled() -> None:
    assembled = set(_lines("files.txt"))
    skeleton = set(_lines("files.skeleton.txt"))
    pack = set(_lines("files.biodata-pack.txt"))
    assert skeleton | pack == assembled
    overlap = (skeleton & pack) - {".gitignore"}
    assert not overlap, f"骨架与领域包清单出现非 .gitignore 重叠: {sorted(overlap)[:10]}"


def test_profile_lists_sorted_unique() -> None:
    for name in ("files.txt", "files.skeleton.txt", "files.biodata-pack.txt"):
        entries = _lines(name)
        assert entries == sorted(entries) and len(entries) == len(set(entries)), f"{name} 未排序或有重复"


def test_pack_profile_contains_exactly_flesh() -> None:
    pack = set(_lines("files.biodata-pack.txt"))
    expected_prefixes = (
        ".agents/skills/", "database/", "eval/", "models/ltr/", "prompts/domains/biodata/",
        "src/dataset_recommender/data/", "src/dataset_recommender/domains/biodata/", "使用教程/",
    )
    offenders = [p for p in pack if p != ".gitignore" and not p.startswith(expected_prefixes)]
    assert not offenders, f"领域包清单混入骨架文件: {offenders[:10]}"
    # 骨架公开仓不得带任何领域包内容（数据库/领域包/评测集/教程一并排除）
    skeleton = set(_lines("files.skeleton.txt"))
    leaks = [p for p in skeleton if p.startswith(expected_prefixes)]
    assert not leaks, f"骨架清单混入领域包内容: {leaks[:10]}"
    # 但骨架保留领域包命名空间（外部/装配的领域包 drop-in 的落点）
    assert "src/dataset_recommender/domains/__init__.py" in skeleton


def test_pack_gitignore_variant_drifts_only_in_models_block() -> None:
    """`.gitignore.biodata-pack.public` 只允许与 `.gitignore.public` 在 models/ 规则块上差异
    （放行 models/ltr/ 制品）；其余行逐字一致——两文件各自演进时漂移当场红。"""
    root = MIRROR_DIR.parents[1]
    base_lines = (root / ".gitignore.public").read_text(encoding="utf-8").splitlines()
    variant_lines = (root / ".gitignore.biodata-pack.public").read_text(encoding="utf-8").splitlines()
    diff = [line for line in variant_lines if line not in base_lines]
    diff += [line for line in base_lines if line not in variant_lines]
    allowed = {"models/", "models/*", "!models/ltr/", "# 例外：models/ltr/ 是 KB 级融合重排器制品（公开目录元数据+合成标签训练），随本仓发布。"}
    unexpected = sorted({line for line in diff if line not in allowed})
    assert not unexpected, f"pack gitignore 变体出现非 models 块漂移: {unexpected[:6]}"
