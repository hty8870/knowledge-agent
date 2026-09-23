import hashlib
from pathlib import Path

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.data_loader import load_raw_records, scan_json_files

BASE_DIR = Path(__file__).resolve().parents[1] / "database" / "base"

# 冻结基准的**内容指纹**（行尾归一化后的 SHA-256；受控重基线时，与下方 test_base_corpus_is_frozen_784
# 的 784、scripts/evaluate_recommendation.py 的 FROZEN_TOP1/5 同一批次同步更新）。
# 补的盲区：计数（test_base_corpus_is_frozen_784 锁 784）与排序指标（冻结评测门锁 97.7/违规0）都看不见
# 「就地改某条 base 记录里不参与评分的字段（URL/md5/description）」——那类静默漂移在此内容门被抓。
# 为什么归一化行尾而非裸字节：签入 blob 是 LF、本机工作树因 autocrlf 是 CRLF，两者内容一致但裸字节
# 不同；裸字节哈希会在不同 checkout（LF/CRLF）间假 FAIL。归一化（\r\n→\n、\r→\n）只锁**内容**、
# 对行尾无关，跨环境稳定，仍能抓住任何真实内容改动。
# 受控重基线（用户授权 7 个 Visium HD CytAssist 新数据集入库）：767→774，指纹同步重算。
# 受控重基线（任务授权：curate sync 运行时 upload 晋升 tracked 快照
# `10x-synced.json`，scripts/promote_uploads.py 首跑 10 条，uid 排序写出）：774→784，
# 10x-Visium.json 指纹逐位不变（策展产物零触碰）。
FROZEN_BASE_SHA256 = {
    "10x-Visium.json": "6bddb3d898c2f0167b1da4607ae440a6c53e6dfc63d12f9d2a840bc8eea581b4",
    "10x-synced.json": "e4caab73b65f78b22dfc01b18d521db0ba29c37a6dfb387a00c0471bb585161a",
}


def _normalized_content_sha256(path: Path) -> str:
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def test_scan_json_files_contains_visium() -> None:
    settings = get_settings()
    files = scan_json_files(settings.data_dir)
    assert files, "database/base 目录下未发现 JSON 文件"
    assert any(path.name == "10x-Visium.json" for path in files)


def test_load_raw_records_non_empty() -> None:
    settings = get_settings()
    records = load_raw_records(settings.data_dir)
    assert len(records) > 0
    assert all(record.source_file.endswith(".json") for record in records[:10])


def test_base_corpus_is_frozen_784() -> None:
    """冻结基准守卫：database/base 必须恰好装载 784 条。
    意外往基准里增删记录（或误把外部库/上传混入）会破坏确定性评测——此测把 784 钉进 pytest，
    不必等官方评测才发现（补 验证指出的『pytest 只查 len>0、未锁 767』覆盖缺口）。
     受控重基线（用户授权 7 个 Visium HD CytAssist 新数据集入库）：767→774。
     受控重基线（任务授权：sync 运行时 upload 晋升 10x-synced.json）：774→784。"""
    settings = get_settings()
    records = load_raw_records(settings.data_dir)
    assert len(records) == 784, f"冻结基准应为 784 条，实为 {len(records)}（database/base 被改动？）"


def test_base_corpus_file_set_and_content_are_frozen() -> None:
    """冻结基准内容守卫：database/base 的文件集与每个文件的**行尾归一化 SHA-256** 必须与冻结清单逐位相符。
    补计数门与评测门都抓不到的一类静默漂移——就地改一条记录里不参与打分的字段（计数仍 784、Top1/Top5
    仍 ≥97.7），内容指纹会立刻不符。哈希前归一化行尾，故对 LF/CRLF checkout 差异免疫、只锁内容。
    受控重基线时同步 FROZEN_BASE_SHA256。"""
    present = sorted(p.name for p in BASE_DIR.glob("*.json"))
    assert present == sorted(FROZEN_BASE_SHA256), (
        f"database/base 文件集变化：现有 {present}，冻结清单 {sorted(FROZEN_BASE_SHA256)}"
        "（新增/删除基准文件属受控重基线，请同步 FROZEN_BASE_SHA256）"
    )
    for name, expected in FROZEN_BASE_SHA256.items():
        actual = _normalized_content_sha256(BASE_DIR / name)
        assert actual == expected, (
            f"database/base/{name} 内容指纹漂移：实为 {actual[:16]}…，冻结为 {expected[:16]}…"
            "（base 被就地改动？受控重基线请同步 FROZEN_BASE_SHA256）"
        )

