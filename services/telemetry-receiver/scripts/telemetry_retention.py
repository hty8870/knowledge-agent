#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遥测数据保留期清理。

删除 ingest_packets 中 received_at 早于「现在 - 保留期」的行（默认 RETENTION_DAYS=90 天）。
应用层 delete 定时方案，不引入重型依赖：

- 手动执行（receiver 容器内，env 齐备）：
    docker compose exec -T receiver python scripts/telemetry_retention.py --days 90
- 定时执行：由部署方按需配置计划任务调用上式（建议加 --quiet 并重定向日志文件）。

--dry-run 只报告不删除；--days 覆盖保留期（默认读取 RETENTION_DAYS 环境配置）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 让 `app` 可导入（脚本位于 services/telemetry-receiver/scripts/ 下，服务目录是父级）
_SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

from app import build_engine, count_expired, ensure_schema, run_retention_once, Settings  # noqa: E402
from telemetry_idempotency import ensure_tables  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="遥测数据保留期清理（默认 90 天）")
    parser.add_argument("--days", type=int, default=None, help="保留期（默认取服务配置 RETENTION_DAYS）")
    parser.add_argument("--dry-run", action="store_true", help="只报告过期行数，不删除")
    parser.add_argument("--quiet", action="store_true", help="不输出非错误信息")
    args = parser.parse_args()

    settings = Settings.from_env()
    days = args.days if args.days is not None else settings.retention_days
    if days <= 0:
        print(f"error: --days 必须为正数，收到 {days}", file=sys.stderr)
        return 2

    settings.retention_days = days
    engine = build_engine(settings.database_url)
    ensure_schema(engine)
    ensure_tables(engine)
    n = count_expired(engine, days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if not args.quiet:
        action = "dry-run 报告" if args.dry_run else "删除"
        print(f"[telemetry-retention] {action}：保留期 {days} 天，cutoff={cutoff.isoformat()}，过期行数={n}")
    if not args.dry_run:
        summary = run_retention_once(engine, settings)
        if not args.quiet:
            print(
                "[telemetry-retention] 已删除 "
                f"packets={summary['deleted_packets']} / exports={summary['deleted_export_files']} / "
                f"export_ok={summary['export_cleanup_ok']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
