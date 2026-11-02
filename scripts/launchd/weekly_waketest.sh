#!/bin/bash
# 週次ジョブの起動・スリープ対策の検証用（2026-11-02）。メールは送らない（weekly_report.sh --dry-run のみ）。
#
# 本番と同じ形（/bin/bash <このスクリプト>、スクリプト内で caffeinate -i -w）で、別ラベルの launchd ジョブから起動して確かめる:
#   - スリープからの予定時刻の復帰後に launchd ジョブが実行されるか（直前の復帰理由を記録）
#   - 実行中にアイドルスリープしないか（10秒ごとの記録に途切れがないか、caffeinate のアサーションがあるか）
#   - 失敗時・実行漏れの再試行（1回目は未完了の週で失敗させ、次の起動で成功するか）
#   - ~/Documents 配下のスクリプトを launchd から読めるか（プライバシー保護による Operation not permitted が出ないか）
#
# 記録: logs/waketest.log（追記）。回数: state/waketest/count。やり直すときは state/waketest/ を消す。
set -uo pipefail

PROJECT_DIR="/Users/kwu/Documents/tcd-seo-report"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs state/waketest
# 実行中のアイドルスリープを防ぐ（本番の weekly_report.sh と同じ方式）。このスクリプトのプロセスが終わると caffeinate も終了する。
# launchd の起動プログラムを caffeinate にすると、~/Documents の読み取りがプライバシー保護で拒否される
# （Operation not permitted）ため、起動プログラムは /bin/bash のままにして、ここで caffeinate を起動する。
/usr/bin/caffeinate -i -w $$ &
LOG="logs/waketest.log"
HEARTBEAT_SECONDS="${WAKETEST_HEARTBEAT_SECONDS:-150}"   # スリープ設定（1分）より長く

COUNT_FILE="state/waketest/count"
n=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$COUNT_FILE"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] run#${n} $*" >> "$LOG"; }

log "=== 開始（PID $$、親 PID $PPID: $(ps -o comm= -p "$PPID")）==="
log "直前の復帰: $(pmset -g log | grep -E '^[0-9-]+ [0-9:]+ [+-][0-9]+ (Wake|DarkWake) ' | tail -1 | cut -c1-160)"
sleep 1
log "スリープ防止のアサーション（このスクリプト分）: $(pmset -g assertions | grep -B1 "on behalf of Process ID $$\$" | tr -s ' \t' ' ' | tr '\n' ' ')"

# 実行中にスリープしないか（途切れ＝スリープ）
start=$(date +%s); prev=$start
while [ $(( $(date +%s) - start )) -lt "$HEARTBEAT_SECONDS" ]; do
  sleep 10
  now=$(date +%s)
  gap=$(( now - prev )); prev=$now
  [ "$gap" -gt 20 ] && log "警告: 記録が ${gap} 秒途切れた（スリープした可能性）"
done
log "心拍 ${HEARTBEAT_SECONDS} 秒: 最大の途切れなし判定は上の警告の有無で確認"

# 再試行の確認: 1回目は未完了の週で失敗させ、2回目以降は送らない dry-run で成功させる
if [ "$n" -eq 1 ]; then
  scripts/weekly_report.sh --dry-run --week 2026-11-02
  rc=$?
  log "失敗の模擬（未完了の週 2026-11-02、dry-run）: 終了コード ${rc}（期待: 1）"
else
  scripts/weekly_report.sh --dry-run --week 2026-10-26
  rc=$?
  log "再試行（2026-10-26、dry-run・送信なし）: 終了コード ${rc}（期待: 0）"
fi
log "最新の週次ログ: $(ls -t logs/weekly_report_*_dryrun.log | head -1)"
log "=== 終了 ==="
