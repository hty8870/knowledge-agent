# -*- coding: utf-8 -*-
"""下载执行器的命令行入口（薄壳）：真实下载一个/一批数据集并逐文件核对 md5/大小。

实现全在 `src/dataset_recommender/corpus/download_executor.py`，这里只解析路径后转发——
与 `scan_lab_assets.py` 对 `lab_ledger` 的关系同构。

  py scripts/provision_dataset.py <dataset_uid>... --out D:/data/provision
  py scripts/provision_dataset.py <uid> --out D:/data --scope all --report-json report.json
  py scripts/record_provision_results.py --report report.json   # 再把实测结果回写台账

默认只下每个数据集 1 个代表性主文件（scope=primary）；巡检旗标文件默认跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dataset_recommender.corpus import download_executor  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(download_executor.main())
