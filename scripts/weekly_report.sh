#!/bin/bash
# 週次SEO/AIOレポート(reports_v2)を生成しunei@tcd.jpへ配信する（2026-09-28にkeiei@から変更）。
# ※2026-09-07以降launchdは無効化済み。手動配信時は宛先を都度確認すること。
# launchd (com.tcd.seo-report.weekly) から毎週月曜9:00に起動される。
# データ取得・検証に失敗した場合は set -e により後続工程(build/send)を実行しない。
set -euo pipefail

PROJECT_DIR="/Users/kwu/Documents/tcd-seo-report"
PYTHON="/usr/bin/python3"
TO_EMAIL="unei@tcd.jp"

cd "$PROJECT_DIR"

mkdir -p logs
LOG_FILE="logs/weekly_report_$(date +%Y%m%d_%H%M%S).log"
exec >>"$LOG_FILE" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 週次レポート開始 ==="

echo "--- [1/4] pipeline: GSC/AIOデータ取得・正規化 ---"
"$PYTHON" -m src_v2.pipeline

echo "--- [2/4] analysis: 分析実行 ---"
"$PYTHON" -m src_v2.analysis.summary

# ②今週の重要判断（重点商談市場・テーマ市場ごとの順位／AIO引用有無）はanalysis.jsonから
# view_model.pyが機械的に組み立てる（2026-09-28以降、claude -pによるnarrative執筆は廃止）。
echo "--- [3/4] build: report.json/md/html生成 ---"
"$PYTHON" -m src_v2.report.build

echo "--- [4/4] send: ${TO_EMAIL} へ送信 ---"
"$PYTHON" -m src_v2.report.send --to "$TO_EMAIL"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 完了 ==="
