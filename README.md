# TCD AIO 日次モニター

SerpAPI で指定クエリの Google AI Overview（AIO）を毎日取得し、履歴として保存するツール。
Looker Studio で可視化できるよう、日次データを CSV へ出力する。

週次・月次の SEO/AIO レポート生成（`generate_report.py`）については [setup.md](setup.md) を参照。

## 1. セットアップ

```bash
cd /Users/kwu/Documents/tcd-seo-report
pip install -r requirements.txt
```

`.env` に SerpAPI のキーを設定する（`.gitignore` 登録済みのためコミットされない）。

```bash
# .env
SERPAPI_API_KEY="xxxxxxxxxxxxxxxx"
```

> 既存の週次レポートツールと同じ環境変数 `SERPAPI_API_KEY` を共有する。

## 2. 監視クエリの設定

`config/aio_queries.yml` に観測したいクエリを登録する。

```yaml
queries:
  - "ブランディング 会社"
  - "ブランディング 会社 東京"
```

同ファイルの `domain_aliases` で、引用元表記のゆれ（社名／英語表記など）を
正規化後のドメインに寄せられる。`tcd_domain`（デフォルト `tcd.jp`）を含むかどうかで
TCD引用・言及を判定する。

## 3. 手動実行

```bash
python3 aio_daily_monitor.py
```

- `--config` : クエリ設定ファイル（デフォルト `config/aio_queries.yml`）
- `--date`   : 観測日を上書き（テスト用、`YYYY-MM-DD`）
- `--daily-json-dir` : 日次JSON出力先（デフォルト `data/aio/daily`）
- `--looker-dir`     : Looker Studio用CSV出力先（デフォルト `data/aio/looker`）
- `--log-file`       : 実行サマリーの追記先（デフォルト `logs/aio_daily.log`）

実行の最後に、以下のような実行サマリーを標準出力に表示し、`logs/aio_daily.log` に追記する。

```
2026-07-07 09:30
8 queries checked
AIO: 6
TCD cited: 0
CSV updated.
```

## 4. 出力ファイル

```
data/aio/
  daily/
    2026-07-06.json        ← その日の生データ（AIO全文・引用URL/ドメイン/タイトル含む）
  looker/
    aio_daily.csv           ← クエリ粒度（date, query, aio_exists, tcd_cited, tcd_mentioned,
                                aio_text_hash, aio_text_changed, status）
    aio_domains_daily.csv   ← 引用ドメイン粒度、1行1ドメイン（date, query, cited_domain,
                                cited_domain_count）
```

- `aio_text_hash` : その日のAIO本文のハッシュ（先頭16桁）
- `aio_text_changed` : 前回観測時からAIO本文が変化したかどうか
- 同じ日に再実行した場合、CSVの該当行は上書きされる（重複しない）

## 5. cron 設定（毎朝6時に自動実行）

```bash
# crontab -e に追加
0 6 * * * cd /Users/kwu/Documents/tcd-seo-report && set -a && source .env && set +a && /usr/bin/python3 aio_daily_monitor.py >> logs/aio_daily_monitor.log 2>&1
```

`logs/` ディレクトリがなければ事前に作成しておく（`mkdir -p logs`）。

## 6. Google Sheets 連携（将来対応）

CSV書き出し部分は `write_csv_rows()` / `update_looker_csvs()` に関数化してある
（`aio_daily_monitor.py`）。Google Sheets へ書き込む場合は、同じ
`(rows, key_cols, column_order)` インターフェースでスプレッドシート書き込み版の
関数を用意し、呼び出し元を差し替えるだけでよい。
