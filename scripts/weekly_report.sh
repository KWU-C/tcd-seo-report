#!/bin/bash
# 週次SEO/AIOレポート(reports_v2)を生成し unei@tcd.jp へ配信する（2026-11-02 改訂: 週次自動運用）。
#
# 期間: GA4・問い合わせ＝前週の月〜日、GSC＝確定済みの最新7日間、AIO＝実行時の観測（src_v2/pipeline.py）。
# 起動: launchd（com.tcd.seo-report.weekly）が 月曜 10:00・11:00・12:00、火・水 10:00 に起動する想定。
#       ※ launchd ジョブは最終承認まで無効のまま（launchctl load しない）。
#
# 二重配信の防止:
#   - ロック（state/weekly_report.lock）で同時実行を防ぐ。
#   - 送信済み台帳（state/sent/<週>.json）に記録がある週は、生成も送信もせずに終了する。
#   - 週は実行日ではなく「前週の月曜」で決めるため、月〜水のどの再試行でも同じ週になる。
# 失敗時: 担当者（NOTIFY_EMAIL）に失敗通知（同じ週・同じ工程は1回だけ）。次の起動時刻に自動で再試行される。
#
# 使い方:
#   scripts/weekly_report.sh                     前週を生成・配信
#   scripts/weekly_report.sh --week 2026-11-02   週を指定（月曜日）
#   scripts/weekly_report.sh --dry-run           送信しない。preview_v2/ に出力（data_v2/・reports_v2/ は変更しない）
#   scripts/weekly_report.sh --refetch           取得済みのデータがあっても取り直す
#   scripts/weekly_report.sh --force-resend      送信済みの週を再送する（明示したときだけ）
#   scripts/weekly_report.sh --to addr@tcd.jp    宛先を変える
#   scripts/weekly_report.sh --test-send [--to addr@tcd.jp]
#                                                テスト送信（既定の宛先は NOTIFY_EMAIL）。dry-run と同じく preview_v2/ に生成し、
#                                                件名に【テスト送信】を付けて送る。記録は state/sent_test/ で、本番の台帳
#                                                state/sent/ と data_v2/・reports_v2/ は変更しない（本番配信がスキップされない）
set -uo pipefail

PROJECT_DIR="/Users/kwu/Documents/tcd-seo-report"
PYTHON="$PROJECT_DIR/.venv/bin/python"
TO_EMAIL="unei@tcd.jp"
NOTIFY_EMAIL="kawauchi@tcd.jp"

# 無人実行ではブラウザ認可を始めない（トークン失効時は失敗として通知する）
export TCD_NONINTERACTIVE=1
export PYTHONWARNINGS="ignore"

WEEK=""
DRY_RUN=0
REFETCH=0
FORCE_RESEND=0
TEST_SEND=0
TO_GIVEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --week) WEEK="$2"; shift 2 ;;
    --to) TO_EMAIL="$2"; TO_GIVEN=1; shift 2 ;;
    --test-send) TEST_SEND=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --refetch) REFETCH=1; shift ;;
    --force-resend) FORCE_RESEND=1; shift ;;
    *) echo "不明な引数: $1" >&2; exit 2 ;;
  esac
done

if [ "$TEST_SEND" -eq 1 ]; then
  [ "$DRY_RUN" -eq 1 ] && { echo "--test-send と --dry-run は同時に指定できない" >&2; exit 2; }
  [ "$TO_GIVEN" -eq 0 ] && TO_EMAIL="$NOTIFY_EMAIL"
  if [ "$(echo "$TO_EMAIL" | tr '[:upper:]' '[:lower:]')" = "unei@tcd.jp" ]; then
    echo "テスト送信は本番の宛先（unei@tcd.jp）には送らない" >&2; exit 2
  fi
fi

cd "$PROJECT_DIR" || exit 1
mkdir -p logs state

# 実行中のアイドルスリープを防ぐ（2026-11-02、A案）。このスクリプトのプロセスが終わると caffeinate も終了する。
# launchd の起動プログラムを caffeinate にすると、~/Documents の読み取りがプライバシー保護で拒否される
# （Operation not permitted）ため、起動プログラムは /bin/bash のままにして、ここで caffeinate を起動する。
/usr/bin/caffeinate -i -w $$ &

SUFFIX=""
[ "$DRY_RUN" -eq 1 ] && SUFFIX="_dryrun"
[ "$TEST_SEND" -eq 1 ] && SUFFIX="_testsend"
# 生成先・通知を本番から切り離すか（dry-run とテスト送信）
PREVIEW=0
{ [ "$DRY_RUN" -eq 1 ] || [ "$TEST_SEND" -eq 1 ]; } && PREVIEW=1
LOG_FILE="$PROJECT_DIR/logs/weekly_report_$(date +%Y%m%d_%H%M%S)${SUFFIX}.log"
exec >>"$LOG_FILE" 2>&1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# 次の自動再試行（launchd の起動時刻: 月 10・11・12時、火・水 10時）
next_retry() {
  local dow hour
  dow=$(date +%u); hour=$((10#$(date +%H)))
  if [ "$dow" -eq 1 ] && [ "$hour" -lt 11 ]; then echo "本日 11:00"
  elif [ "$dow" -eq 1 ] && [ "$hour" -lt 12 ]; then echo "本日 12:00"
  elif [ "$dow" -eq 1 ]; then echo "火曜 10:00"
  elif [ "$dow" -eq 2 ]; then echo "水曜 10:00"
  else echo ""
  fi
}

# ── 週の決定 ──────────────────────────────────────────
if [ -z "$WEEK" ]; then
  WEEK=$("$PYTHON" -c "from src_v2.pipeline import get_report_week; print(get_report_week()['report_week_start'])") || { log "週を決定できない"; exit 1; }
fi
WEEK_END=$("$PYTHON" -c "from datetime import date; from src_v2.pipeline import get_report_week; print(get_report_week(week_start=date.fromisoformat('$WEEK'))['report_week_end'])") || { log "週の指定が不正（月曜日を YYYY-MM-DD で指定）: $WEEK"; exit 2; }
FOLDER="${WEEK}_${WEEK_END}"

if [ "$PREVIEW" -eq 1 ]; then
  PROCESSED_ROOT="preview_v2/processed"
  REPORTS_ROOT="preview_v2/reports"
else
  PROCESSED_ROOT="data_v2/processed"
  REPORTS_ROOT="reports_v2"
fi

log "=== 週次レポート開始 週=${FOLDER} dry-run=${DRY_RUN} test-send=${TEST_SEND} 宛先=${TO_EMAIL} ==="

# ── ロック（同時実行の防止） ──────────────────────────────
LOCK_DIR="state/weekly_report.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  OTHER_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$OTHER_PID" ] && kill -0 "$OTHER_PID" 2>/dev/null; then
    log "別の実行（PID ${OTHER_PID}）が動いているため終了する"
    exit 0
  fi
  log "古いロックを削除する（PID ${OTHER_PID:-不明} は終了済み）"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" || { log "ロックを取得できない"; exit 1; }
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# ── 送信済みなら何もしない ─────────────────────────────────
if [ "$PREVIEW" -eq 0 ] && [ "$FORCE_RESEND" -eq 0 ]; then
  if "$PYTHON" -c "import sys; from pathlib import Path; from src_v2.report.send import already_sent; sys.exit(0 if already_sent(Path('$REPORTS_ROOT/$FOLDER'), '$TO_EMAIL') else 1)"; then
    log "${FOLDER} は ${TO_EMAIL} へ送信済み（state/sent/${FOLDER}.json）。終了する"
    exit 0
  fi
fi

fail() {
  local step="$1"
  log "失敗: ${step}"
  local flags=()
  [ "$PREVIEW" -eq 1 ] && flags+=(--dry-run)
  "$PYTHON" -m src_v2.notify --kind failure --week "$WEEK" --to "$NOTIFY_EMAIL" \
    --step "$step" --log "$LOG_FILE" --next-retry "$(next_retry)" ${flags[@]+"${flags[@]}"}
  exit 1
}

# ── 1. 取得・検証・正規化 ─────────────────────────────────
if [ "$PREVIEW" -eq 1 ]; then
  # 前週比・継続性の判定に過去の処理済みデータが要るため、JSON だけを preview_v2/ に写す（data_v2/ は変更しない）
  mkdir -p "$PROCESSED_ROOT"
  rsync -a --include='*/' --include='normalized.json' --include='analysis.json' --exclude='*' \
    data_v2/processed/ "$PROCESSED_ROOT/" || fail "dry-run 準備"
fi

if [ "$REFETCH" -eq 0 ] && [ -f "$PROCESSED_ROOT/$FOLDER/normalized.json" ]; then
  # 再試行時に SerpAPI を重ねて呼ばない（取得は成功し、生成・送信で失敗した場合）
  log "[1/4] 取得済みのデータを使う（$PROCESSED_ROOT/$FOLDER/normalized.json。取り直す場合は --refetch）"
else
  log "[1/4] pipeline: GSC・AIO・GA4・問い合わせ集計の取得・検証・正規化"
  "$PYTHON" -m src_v2.pipeline --week "$WEEK" --output-root "$PROCESSED_ROOT" || fail "データ取得・検証"
fi

# ── 2. 分析 ──────────────────────────────────────────────
log "[2/4] analysis"
"$PYTHON" -m src_v2.analysis.summary --period-dir "$PROCESSED_ROOT/$FOLDER" || fail "分析"

# ── 3. レポート生成 ───────────────────────────────────────
log "[3/4] build"
"$PYTHON" -m src_v2.report.build --analysis-path "$PROCESSED_ROOT/$FOLDER/analysis.json" \
  --output-root "$REPORTS_ROOT" || fail "レポート生成"

# ── 4. 送信 ──────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  log "[4/4] dry-run のため送信しない（$REPORTS_ROOT/$FOLDER/report.html）"
else
  log "[4/4] send: ${TO_EMAIL}"
  SEND_FLAGS=()
  [ "$FORCE_RESEND" -eq 1 ] && SEND_FLAGS+=(--force-resend)
  [ "$TEST_SEND" -eq 1 ] && SEND_FLAGS+=(--test)
  "$PYTHON" -m src_v2.report.send --report-dir "$REPORTS_ROOT/$FOLDER" --to "$TO_EMAIL" ${SEND_FLAGS[@]+"${SEND_FLAGS[@]}"}
  rc=$?
  if [ "$rc" -eq 3 ]; then
    log "送信済みのため送らない"
    exit 0
  elif [ "$rc" -ne 0 ]; then
    fail "メール送信"
  fi
fi

# ── 配信後の確認通知（要確認の項目がある場合のみ） ─────────────
REVIEW_FLAGS=()
[ "$PREVIEW" -eq 1 ] && REVIEW_FLAGS+=(--dry-run)
"$PYTHON" -m src_v2.notify --kind review --week "$WEEK" --to "$NOTIFY_EMAIL" --delivered-to "$TO_EMAIL" \
  --report-dir "$REPORTS_ROOT/$FOLDER" ${REVIEW_FLAGS[@]+"${REVIEW_FLAGS[@]}"} || log "確認通知を送れなかった（配信は完了）"

log "=== 完了 ==="
