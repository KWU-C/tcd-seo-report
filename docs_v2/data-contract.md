# TCD週次SEO/AIOレポート v2 データコントラクト

`docs_v2/requirements.md` の実装仕様。raw保存・正規化・分析・レポート出力の各段階で、どのファイル/テーブルがどのスキーマを持つかを固定する。ここに書いた構造を変更する場合は、レポート・JSON・将来のダッシュボードが同じ定義を参照している前提を崩さないよう、本ファイルを先に更新してから実装を変える。

---

## 1. 保存階層

```text
data_v2/raw/gsc/        Search Console APIレスポンスをそのまま保存（加工前）
data_v2/raw/aio/        AIO観測結果をそのまま保存（加工前）
data_v2/raw/ga4/        GA4を含める場合のみ使用（6.3節オープン事項）
data_v2/processed/      正規化済みデータ（レポートと将来のダッシュボードが共通参照）
```

rawは同一期間のレポートを再現できることが目的なので、取得日時・リクエストパラメータを含めて保存する（値の上書き・削除をしない。同一期間を再取得した場合は別ファイルとして追記する）。

## 2. Raw データスキーマ

### 2.1 GSC raw

ファイル: `data_v2/raw/gsc/{fetched_at}_{dimension_key}_{start}_{end}.json`

```yaml
fetched_at: ISO8601                 # 取得実行時刻
site_url: string                    # 対象プロパティ（tcd.jp）
dimensions: [date] | [date, query] | [date, page] | [date, page, query]
date_start: YYYY-MM-DD
date_end: YYYY-MM-DD
row_limit: int
response:                           # Search Console APIレスポンスをそのまま格納
  rows: [...]
  responseAggregationType: string
```

### 2.2 AIO raw

ファイル: `data_v2/raw/aio/{observed_at}_{query_hash}.json`

```yaml
observed_at: ISO8601
provider: serpapi
query: string
acquisition_status: success | not_generated | not_observed | failed | not_run
raw_response: object | null         # 成功時はSerpAPIレスポンスをそのまま格納。failed時はnull可
error_message: string | null
```

`not_run`（そもそも観測を実行しなかった週）と`failed`（実行したが取得エラー）を保存時点で区別する。どちらも`raw_response: null`になり得るが、`acquisition_status`で判別する。

### 2.3 GA4 raw（6.3節の判断待ち。含める場合のみ有効）

```yaml
fetched_at: ISO8601
property_id: string
date_start: YYYY-MM-DD
date_end: YYYY-MM-DD
dimensions: [...]
metrics: [...]
response: object
```

## 3. URL正規化ルール

正規化関数 `normalize_url(raw_url) -> NormalizedUrl` の入出力契約。

```yaml
input: string                       # GSC/AIO/pages.yamlいずれかから来た生URL
output:
  normalized_url: string            # https + host統一 + 末尾スラッシュ規則統一後
  canonical_url: string | null      # 判明していればcanonical。未確認はnull（推測しない）
  is_excluded: bool                 # preview/customize等の除外対象か
  exclusion_reason: string | null
```

処理内容（すべて必須）:

1. http → https 統一
2. host統一（www有無等、実サイトの正規ホストに合わせる）
3. URLエンコード/デコード表現差の統一
4. 末尾スラッシュ・`index.html`等の統一
5. 不要クエリパラメータ除去（`customize_changeset_uuid`等のWordPressプレビュー系を含む）
6. canonical URLの解決（HTTP応答から確認できた場合のみ設定。未確認は`canonical_url: null`のまま扱う）
7. preview/customize URLの除外フラグ付与

**禁止事項:** ハイフンとアンダースコアを自動的に同一視しない。`/service/about_branding_company.html` と `/service/about-branding-company.html` が両方観測された場合、実在URL・HTTP応答・canonical・リダイレクトを確認してから同一/別の判断をする。自動マージしない。

## 4. 正規化済み（processed）データスキーマ

### 4.1 GSC系ファクトテーブル

```yaml
table: gsc_query_daily
columns:
  - date: DATE
  - query: string
  - normalized_query: string        # 表記揺れ統一（完全一致判定用）
  - clicks: int
  - impressions: int
  - ctr: float
  - position: float
  - source_fetched_at: ISO8601      # 由来rawファイルへのトレーサビリティ

table: gsc_page_daily
columns:
  - date: DATE
  - page_url: string                # 正規化前（GSC生値）
  - normalized_url: string
  - page_id: string | null          # config_v2/pages.yaml の id に解決できた場合のみ設定
  - clicks: int
  - impressions: int
  - ctr: float
  - position: float
  - source_fetched_at: ISO8601

table: gsc_page_query_daily
columns:
  - date: DATE
  - page_url: string
  - normalized_url: string
  - page_id: string | null
  - query: string
  - clicks: int
  - impressions: int
  - ctr: float
  - position: float
  - source_fetched_at: ISO8601
```

`page_id`が`null`になるのは「pages.yamlに未登録のページ」を意味する。これはエラーではなく、「定点観測対象外」として区別してレポートする（`config_v2/pages.yaml`の`enabled: false`や未登録ページと、取得失敗を混同しない）。

### 4.2 AIO正規化テーブル

```yaml
table: aio_observation
columns:
  - observed_at: ISO8601
  - query: string
  - acquisition_status: success | not_generated | not_observed | failed | not_run
  - aio_generated: bool | null       # success=true / not_generated=false / それ以外はnull
  - tcd_cited: bool | null           # aio_generated=true の場合のみ判定。false/nullではnull
  - tcd_urls: [string]
  - cited_domains: [string]
  - cited_urls: [string]             # Google経由のリンク（google.com/searchviewer、google.co.jp/goto等）は遷移先が分からないため含めない
  - unknown_sources: [string]        # 引用元ドメインを特定できなかった引用のsource表記（Google経由リンクで、sourceがドメインでも登録済み別名でもない場合）。推測でドメインに寄せない
  - answer_text_hash: string | null  # 同条件の成功観測間でのみ比較に使う
  - error_message: string | null
```

週次比較時は `acquisition_status == success` の観測同士でのみ差分を取る。`not_run`/`failed`が混在する週は、その週のAIO変化を「判断保留」として扱い、断定しない。

### 4.3 ページマスタ結合

`config_v2/pages.yaml` の `pages[].id` を `page_id` として上記テーブルに結合する。結合キーは正規化済みURL（`normalized_url`）で、pages.yaml側のURLも同じ`normalize_url()`を通してから比較する（表記の違いで結合漏れを起こさないため）。

## 5. データ品質チェック出力

```yaml
table: data_quality_report
columns:
  - run_at: ISO8601
  - status: pass | warning | fail
  - warnings: [string]
  - errors: [string]
  - latest_confirmed_gsc_date: DATE
  - current_period: {start: DATE, end: DATE}
  - comparison_period: {start: DATE, end: DATE}
  - aio_success_count: int
  - aio_failed_count: int
  - aio_not_run_count: int
  - priority_query_fetch_condition_matches_prev_week: bool
```

`status: fail` の場合、通常のレポート生成を中断し、このテーブルの内容のみを出力する（requirements.md 8節）。

## 6. 分析結果JSON（analysis.json）【Phase 4で確定・実装済み】

Phase 4（`src_v2/analysis/`）は、5節までの正規化済みデータを入力に、**Finding単位**で分析結果を出力する。HTML・Markdownなどのレポート生成（Phase 5）は、この`analysis.json`のみを参照し、`normalized.json`やrawを直接参照しない。

```yaml
analysis:
  schema_version: string             # SCHEMA_VERSION（src_v2/schema.py）
  generated_at: ISO8601
  period: {cur_start: DATE, cur_end: DATE, prv_start: DATE, prv_end: DATE}
  data_quality: data_quality_report  # 5節と同一構造をそのまま埋め込み
  weeks_available: int               # data_v2/processed/ に蓄積済みの週数（Confidence判定に使用）
  findings: [Finding]                # 下記
  counts: {total: int, by_severity: object, by_category: object}
```

### Finding

```yaml
Finding:
  # 必須
  id: string                  # 決定的・一意（例: market-branding-tokyo, page-service-branding, initiative-osaka-link-strengthening）
  category: market | competitor | page | initiative
  severity: critical | warning | info
  confidence: High | Medium | Low | Not assessable
  message: string              # 観測事実（日本語）
  recommendation: string       # 推奨アクション（日本語）
  # 追加（追跡可能性のため付与。省略可だが実装では常に付与している）
  judgment: 実施 | 継続観測 | 効果確認待ち | 再調査 | 判断保留
  evidence: object             # 根拠となった生の集計値（impressions/clicks/position等）。結論から根拠まで追えるようにする
  source: object                # 対象（query_id / page_id / initiative名）
```

責務の分離（実装済み、`src_v2/analysis/`配下）:

- `confidence.py` — Confidence判定のみを行う純粋関数。データ量・継続週数・SEO/AIO一致・ページ/クエリ一致のうち、該当する材料のみで判定する（該当しない材料はNoneとして無視し、Not assessableへ寄せない）
- `common.py` — Finding生成ヘルパー（必須フィールドのバリデーション込み）、週次集計（`sum_metrics`/`group_metrics`）
- `market.py` — `config_v2/queries.yaml`の`markets`（4重点市場）について、SEO状態・AIO状態・AIO競合引用の新規/消失をFinding化する。
  **2026-07-27改修**: マッチングは`markets.*.all`のトークンAND一致（`normalize.matches_all_tokens`、順序不問）に一本化した（完全一致／関連クエリ群の2分類は廃止）。
  evidence構造: `current`/`comparison`（AND一致行の合算） / `matched_query_variants`（一致した生クエリごとの内訳、根拠追跡用） / `competitor_summary`。
  競合差分は前週の`normalized.json`が存在する場合のみ算出する（存在しない週は算出自体をスキップし、Not assessableを偽装しない）
- `pages.py` — `config_v2/pages.yaml`の`enabled`ページについて、SEO状態とAIO被引用をFinding化する。マガジン上位ランディングページ（動的抽出対象）はここでは扱わない
- `landing_pages.py` — `/branding/`配下でpage_id未解決（pages.yaml未登録）かつ`config_v2/excluded_landing_pages.yaml`の除外URLに該当しないページを、クリック数降順でTOP N件抽出する（category: `magazine_landing_page`）
- `initiatives.py` — `docs/current-initiatives.md`をパースし、実装日ベースで14日/28日評価のタイミングを判定する。実装日`unknown`の施策は評価自体をスキップする（日付を推測しない）
- `summary.py` — 上記を呼び出し、Findingを集約して`analysis.json`を書き出すオーケストレーター（`data_v2/processed/{period}/analysis.json`）

## 7. 再現性

- 同じ`period.start`/`period.end`を指定すれば、`data_v2/raw/`の該当ファイルから同一の`normalized.json`・`analysis.json`を再生成できること。
- rawファイルが存在しない期間は「再取得」であり「過去データの上書き」ではない（rawは追記のみ、削除しない）。
