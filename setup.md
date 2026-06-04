# TCD SEO/AIO レポート生成ツール — セットアップ手順

## 1. 依存ライブラリのインストール

```bash
cd /Users/kwu/Documents/tcd-seo-report
pip install -r requirements.txt
```

## 2. Google Search Console API の有効化

1. [Google Cloud Console](https://console.cloud.google.com/) を開く
2. プロジェクトを作成（または既存プロジェクトを選択）
3. 「APIとサービス」→「ライブラリ」で **Google Search Console API** を有効化

## 3. OAuth2 認証情報の作成

1. 「APIとサービス」→「認証情報」→「認証情報を作成」→「OAuth 2.0 クライアント ID」
2. アプリケーションの種類: **デスクトップ アプリ**
3. 作成後、「JSON をダウンロード」をクリック
4. ダウンロードしたファイルを `credentials.json` にリネームしてプロジェクトルートに配置

> **注意**: `credentials.json` と `token.pickle` は `.gitignore` に登録済みです。絶対にリポジトリにコミットしないでください。

## 4. 初回認証

```bash
python generate_report.py
```

初回実行時にブラウザが開き、kawauchi@tcd.jp の Google アカウントで認証を求めます。
認証後、`token.pickle` が自動生成されます。次回以降はブラウザ認証不要です。

### token.pickle の再発行

トークンの有効期限切れ・権限変更時:

```bash
rm token.pickle
python generate_report.py
```

## 5. 設定ファイルを確認

`config/aio_monitor.yml` で以下を編集:

- `site_url`: 対象サイトの URL（現在: `https://tcd.jp/`）
- `key_keywords`: 重点キーワード一覧
- `aio_focus_pages`: AIO 監視ページ（サイト内パス）
- `definition_articles`: 〜とは系コンテンツのパス
- `lp_cv_pages`: LP・CV ページのパス

## 6. 定期実行（本番運用）

### 毎週月曜 9:00 に自動送信

```bash
# crontab -e に追加
0 9 * * 1 cd /Users/kwu/Documents/tcd-seo-report && \
  source .env && \
  python3 generate_report.py --period auto --send-email --to kawauchi@tcd.jp
```

### --period auto の動作

| 実行日 | 判定 | 対象期間 | 比較期間 |
|--------|------|----------|----------|
| 月の 1〜7 日かつ月曜 | monthly | 前月 1 日〜前月末日 | 前々月 1 日〜前々月末日 |
| それ以外の月曜 | weekly | 先週月〜日 | 先々週月〜日 |

### 手動テスト用コマンド

```bash
# 週次（任意の日に実行）
python3 generate_report.py --period weekly --send-email --to kawauchi@tcd.jp

# 月次（任意の日に実行）
python3 generate_report.py --period monthly --send-email --to kawauchi@tcd.jp

# メール送信なしでレポート確認
python3 generate_report.py --period auto
open reports/*.html
```

## 7. OpenAI API キー設定

```bash
# 方法A: 環境変数（セッション限定）
export OPENAI_API_KEY="sk-..."

# 方法B: .env ファイル（推奨・pip install python-dotenv が必要）
echo 'OPENAI_API_KEY="sk-..."' > .env
pip3 install python-dotenv
```

`.env` は `.gitignore` 登録済みのため、誤ってコミットされません。

## 8. レポート生成（その他）

```bash
# credentials.json のパスを明示的に指定
python3 generate_report.py --credentials-file /path/to/credentials.json

# HTML 出力なし
python3 generate_report.py --no-html

# 出力先変更
python3 generate_report.py --output-dir /path/to/output
```

## 7. 出力ファイル

```
reports/
  2026-06-seo-aio-report.md   ← Markdown
  2026-06-seo-aio-report.html ← HTML（ブラウザで開く）
```

## レポート構成

| セクション | 内容 |
|-----------|------|
| 1. サイト全体サマリー | クリック・IMP・CTR・順位の今期/前期比較 |
| 2. 重点キーワード分析 | 指定 KW ごとの詳細比較 |
| 3. AIO 重点ページ分析 | AIO スコア付きページ別比較 |
| 4. Definition 記事分析 | 〜とは記事の CTR・順位動向 |
| 5. LP/CV ページ分析 | コンバージョン関連ページの状況 |
| 6. デバイス別分析 | PC / Mobile / Tablet 別集計 |
| 7. AIO シグナル検出 | CTR 損失が疑われるクエリ一覧 |
| 8. 上位クエリ TOP20 | クリック数順の主要クエリ |
| 9. 上位ページ TOP20 | クリック数順の主要ページ |
| 10. 注目ポイント・改善推奨 | 自動生成のインサイトまとめ |

## AIO スコアについて

```
AIOスコア = 1 - (実際のクリック数 / 掲載順位から期待されるクリック数)
```

- 🟢 低 (< 0.4): 期待通りのクリックを獲得
- 🟡 中 (0.4〜0.6): AI Overview の影響の可能性あり
- 🔴 高 (> 0.6): AI Overview による CTR 損失の可能性が高い

期待 CTR の基準値（Backlinko 2023 調査）:
1位: 28.4% / 2位: 15.2% / 3位: 10.7% / 4位: 7.9% / 5位: 6.0% ...
