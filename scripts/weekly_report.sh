#!/bin/bash
# 週次SEO/AIOレポート(reports_v2)を生成しkeiei@tcd.jpへ配信する。
# launchd (com.tcd.seo-report.weekly) から毎週月曜9:00に起動される。
# データ取得・検証に失敗した場合は set -e により後続工程(build/send)を実行しない。
set -euo pipefail

PROJECT_DIR="/Users/kwu/Documents/tcd-seo-report"
PYTHON="/usr/bin/python3"
CLAUDE="/Users/kwu/.local/bin/claude"
TO_EMAIL="keiei@tcd.jp"

cd "$PROJECT_DIR"

mkdir -p logs
LOG_FILE="logs/weekly_report_$(date +%Y%m%d_%H%M%S).log"
exec >>"$LOG_FILE" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 週次レポート開始 ==="

echo "--- [1/5] pipeline: GSC/AIOデータ取得・正規化 ---"
"$PYTHON" -m src_v2.pipeline

echo "--- [2/5] analysis: 分析実行 ---"
"$PYTHON" -m src_v2.analysis.summary

PERIOD_DIR=$(find data_v2/processed -mindepth 1 -maxdepth 1 -type d -name '*_*' | sort | tail -1)
ANALYSIS_PATH="${PERIOD_DIR}/analysis.json"
NARRATIVE_PATH="${PERIOD_DIR}/narrative.txt"

if [ ! -f "$ANALYSIS_PATH" ]; then
  echo "エラー: ${ANALYSIS_PATH} が見つかりません。中断します。"
  exit 1
fi

echo "--- [3/5] narrative: Executive Summary執筆 (claude -p) ---"
"$CLAUDE" -p "あなたはTCD.jpの週次SEO/AIOレポートにおける経営者向け要約(①今週の結論／Executive Summary)を執筆するタスクを行います。

1. ${ANALYSIS_PATH} を読み込んでください。schema_version, period, data_quality, weeks_available, ga4_summary, findings(market/competitor/page/magazine_landing_page/initiativeの各category、severity、confidence、judgment、message、evidence等)を含む分析結果です。
2. 経営者向けに、日本語で3ブロックの文章を、必ずこの順番で執筆してください。社内の意思決定者向けなので、各ブロックは短く簡潔にする（説明の重複・毎回同じ定型句の繰り返しを避ける）。表示順がアルファベット順と一致しないため、各ブロックの見出しはA/B/C等の記号を使わず「●」で統一する:
   - ● 今週の評価: 重点対応がある場合、対応ごとに3行で書く（フォーマットは4番を参照）。重点対応がなければ『断定的な施策変更を行うタイミングではない』の1文のみ
   - ● 市場存在感: 重点4市場（ブランディング会社 東京/大阪、インナーブランディング会社 東京/大阪）それぞれの状態を一言で（横ばい/改善/悪化/未露出）。重点市場のクエリベース順位・表示回数は狭い重点クエリ群のみの数値であり、対応する地域LPページ（config_v2/pages.yamlでobjectiveにregionalを含むページ。現時点では大阪ページ https://tcd.jp/branding/03-news/osaka-branding-company.html）全体のGSC実績（analysis.jsonのfindings, category=pageまたはmagazine_landing_page）とは別の指標。両者に乖離がある場合（例: 重点クエリの順位は横ばいでもLPページ全体のクリック・表示回数が増加している等）は、経営判断の材料として両方を区別して一言添える（クエリ順位だけで市場を語らない）
   - ● AIO: 4市場中何件でTCDが引用されているかを1文。競合ドメインの動向は『④重点市場を参照』で済ませ、詳細を書かない（継続性が確認できているものだけを『参入』と呼ぶ。1回だけの新規引用は参入と書かない）
3. 禁止事項（厳守）:
   - データから判断できない原因を断定しない
   - 1週だけの変化を『トレンド』『傾向』と表現しない
   - GSC表示回数0を『検索需要が存在しない』と表現しない（『TCDが表示機会を得られていない』と『需要がない』は別物であり、外部の検索ボリュームデータ等で需要ゼロが確認できない限り需要断定はしない）
   - AIOへの新規引用1回だけを『競合参入』『参入した』と断定しない（analysis.jsonのcompetitor Findingでseverity=warningになっているものは継続性確認済み、severity=infoは単発で継続性未確認）
   - Low Confidenceのページ順位変化から、すぐに本文修正・具体的な改修提案をしない
   - 同じ説明・同じ注記文を複数箇所で繰り返さない
4. フォーマット（厳守）:
   - 各ブロックは見出し行（例: 『● 市場存在感』）の直後で改行し、本文は次の行から書く。見出しと本文を『 — 』や読点でつなげて同じ行に書かない
   - 『今週の評価』ブロックの対応ごとの3行は次の形式にする（『→』の前には全角スペース1つを置き、上の『・』と縦位置を揃える）:
     ・対象（観測値）
     　→評価（1文）
     　→次（具体的な確認対象。抽象語で終わらせず、確認する対象URLやデータの種類まで書く）
   - 対応が複数ある場合は、対応と対応の間に空行を1行入れる
5. 執筆した文章のみを ${NARRATIVE_PATH} に保存してください。見出しは指定の3ブロック（今週の評価／市場存在感／AIO、この順）のみとし、それ以外のブロック（SEO・サイト施策・総合判断等）は書かないでください。前置き・余計な箇条書き記号も付けず、本文の段落のみを書いてください。" \
  --permission-mode acceptEdits \
  --allowedTools "Read Write" \
  --add-dir "$PROJECT_DIR" \
  --no-session-persistence \
  --output-format text

if [ ! -s "$NARRATIVE_PATH" ]; then
  echo "エラー: ${NARRATIVE_PATH} が生成されませんでした。中断します。"
  exit 1
fi

echo "--- [4/5] build: report.json/md/html生成 ---"
"$PYTHON" -m src_v2.report.build --narrative-file "$NARRATIVE_PATH"

echo "--- [5/5] send: ${TO_EMAIL} へ送信 ---"
"$PYTHON" -m src_v2.report.send --to "$TO_EMAIL"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 完了 ==="
