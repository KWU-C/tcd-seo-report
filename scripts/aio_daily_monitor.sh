#!/bin/bash
# AIO日次モニター(aio_daily_monitor.py)を実行する。
# launchd (com.tcd.seo-report.aio-daily) から毎日9:30に起動される。
# 旧cron登録(30 9 * * *)からの移行版。設計はscripts/weekly_report.shに合わせている。
set -euo pipefail

PROJECT_DIR="/Users/kwu/Documents/tcd-seo-report"
PYTHON="/usr/bin/python3"

cd "$PROJECT_DIR"

mkdir -p logs
LOG_FILE="logs/aio_daily_$(date +%Y%m%d_%H%M%S).log"
exec >>"$LOG_FILE" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') AIO日次モニター開始 ==="

"$PYTHON" aio_daily_monitor.py

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 完了 ==="
