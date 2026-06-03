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

## 3. 認証情報の設定（どちらか一方）

### 方法A: サービスアカウント（自動化・定期実行向け）

1. 「APIとサービス」→「認証情報」→「サービスアカウントを作成」
2. JSON キーをダウンロード（例: `sa-key.json`）
3. 環境変数を設定:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/sa-key.json"
   ```
4. Search Console の **設定 → ユーザーと権限** でサービスアカウントのメールを追加
   （権限: 制限付き または フル）

### 方法B: OAuth2（手動実行向け）

1. 「APIとサービス」→「認証情報」→「OAuth 2.0 クライアント ID」を作成
2. アプリケーションの種類: **デスクトップ アプリ**
3. JSON をダウンロードし `credentials.json` としてプロジェクトルートに配置
4. 初回実行時にブラウザで認証 → `token.pickle` が自動生成される

## 4. 設定ファイルを編集

`config/aio_monitor.yml` を開いて以下を編集:

- `site_url`: 対象サイトのURL
- `key_keywords`: 重点キーワード一覧
- `aio_focus_pages`: AIO監視ページ（サイト内パス）
- `definition_articles`: 〜とは系コンテンツのパス
- `lp_cv_pages`: LP・CVページのパス

## 5. レポート生成

```bash
# 基本実行（Markdown + HTML を reports/ に出力）
python generate_report.py

# サービスアカウントを直接指定
python generate_report.py --credentials /path/to/sa-key.json

# HTML 出力なし
python generate_report.py --no-html

# 出力先変更
python generate_report.py --output-dir /path/to/output
```

## 6. 出力ファイル

```
reports/
  2026-06-seo-aio-report.md   ← Markdown
  2026-06-seo-aio-report.html ← HTML（ブラウザで開く）
```

## レポート構成

| セクション | 内容 |
|-----------|------|
| 1. サイト全体サマリー | クリック・IMP・CTR・順位の今期/前期比較 |
| 2. 重点キーワード分析 | 指定KWごとの詳細比較 |
| 3. AIO重点ページ分析 | AIOスコア付きページ別比較 |
| 4. Definition記事分析 | 〜とは記事のCTR・順位動向 |
| 5. LP/CVページ分析 | コンバージョン関連ページの状況 |
| 6. デバイス別分析 | PC / Mobile / Tablet 別集計 |
| 7. AIOシグナル検出 | CTR損失が疑われるクエリ一覧 |
| 8. 上位クエリ TOP20 | クリック数順の主要クエリ |
| 9. 上位ページ TOP20 | クリック数順の主要ページ |
| 10. 注目ポイント・改善推奨 | 自動生成のインサイトまとめ |

## AIOスコアについて

```
AIOスコア = 1 - (実際のクリック数 / 掲載順位から期待されるクリック数)
```

- 🟢 低 (< 0.4): 期待通りのクリックを獲得
- 🟡 中 (0.4〜0.6): AI Overview の影響の可能性あり
- 🔴 高 (> 0.6): AI Overview によるCTR損失の可能性が高い

期待CTRの基準値（Backlinko 2023調査）:
1位: 28.4% / 2位: 15.2% / 3位: 10.7% / 4位: 7.9% / 5位: 6.0% ...
