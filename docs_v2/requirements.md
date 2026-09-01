# TCD週次SEO/AIOレポート v2 要件定義

正本: `CLAUDE_CODE_generate_report_v2_制作指示書.md`（プロジェクトルート）
本ファイルは、その制作指示書とPhase 0調査・以降の合意事項をもとに、実装時点で有効な要件を確定したもの。指示書と本ファイルが矛盾する場合は、本ファイル（合意済みの最新決定）を優先する。

---

## 1. 目的

TCDサイトのSEO・AIO上の市場存在感を毎週同じ基準で確認し、競合との差分を把握し、次のサイト施策を判断する。

レポートが答えるべき問い:

1. 重点市場でTCDの存在感は上がったか、下がったか。
2. SEO順位、表示、流入はどう変化したか。
3. AIOでTCDは引用されているか。競合は誰か。
4. 重点ページは狙った検索意図で認識されているか。
5. 実施済み施策は効果確認段階か、再改善が必要か。
6. 今週は実施、継続観測、判断保留のどれか。

## 2. スコープ方針

- 既存 `generate_report.py` は削除・上書きしない。既存の定期実行（`aio_daily_monitor.py`の日次cronのみ。`generate_report.py`自体はcron化されていない）も変更しない。
- v2は同一リポジトリ内に `_v2` サフィックスで分離実装する（Phase 0調査で依存関係・cron衝突なしを確認済み）。
  - `config_v2/`（作成済み: `pages.yaml`）
  - `docs_v2/`（本ファイル）
  - 以降作成予定: `src_v2/`, `data_v2/`, `reports_v2/`, `tests_v2/`
- 最低2〜4週間、既存レポートとv2を並行生成し、数値再現性・欠損の扱い・判断妥当性・可読性・実行時間・保守性を比較してから既存系の停止を検討する。
- Looker Studioは正式データソースにしない（Search Console・AIOは直接取得、GA4は範囲未確定＝下記オープン事項）。

## 3. 重点市場クエリ

【2026-07-27 方針転換：完全一致（exact match）を廃止し、AND条件（トークン一致・語順不問）を唯一の正とする】

背景: Looker Studio上の集計値との突合作業で、「ブランディング会社 東京」の完全一致だけでは
「東京 ブランディング会社」「ブランディング会社 東京」「東京中央区 ブランディングデザイン会社」等、
Google上で実際に使われている語順・表記違いのクエリを大量に取りこぼすことが判明した
（完全一致=数十件のImpressionに対し、AND条件では実測で3倍前後に増加するケースを確認）。
一方、Looker側のRE2正規表現フィルタ（`.*インナーブランディング.*(会社|支援|東京).*`等、OR条件）は
「インナーブランディング 支援」のような無関係なクエリを東京・大阪両方に二重計上する欠陥があり、
Looker側を修正するより「AND条件を唯一の正とし、Lookerは過去比較の参考程度に格下げする」方針とした。

`config_v2/queries.yaml` の `markets`（旧 `priority_market_queries`）で定義する:

```yaml
markets:
  branding_tokyo:
    query: "ブランディング 会社 東京"   # AIO観測・表示用の代表クエリ文字列
    market: branding
    region: tokyo
    all: [ブランディング, 会社, 東京]    # GSCマッチングの正。順序不問のAND条件
  branding_osaka: {..., all: [ブランディング, 会社, 大阪]}
  inner_tokyo:    {..., all: [インナーブランディング, 会社, 東京]}
  inner_osaka:    {..., all: [インナーブランディング, 会社, 大阪]}
```

マッチングルール: `src_v2/normalize.py` の `matches_all_tokens(gsc_query, tokens)` が
`all(normalize(token) in normalize(gsc_query) for token in tokens)` を実装する
（`classify_query_match`の完全一致/関連クエリ群の2分類は廃止）。

一致した実クエリの内訳は `market.py` が `evidence.matched_query_variants` に必ず保持する
（「集計結果がどの生クエリ由来か分からない」状態を作らないため。レポート②の市場カードに
「一致した実クエリ内訳」として表示）。

各市場で確認: SEO順位 / Impression / Click / CTR / 表示URL / 前週比 / 4週推移 / AIO回答生成有無 / TCD引用有無 / TCD引用URL / 引用競合ドメイン（増減トレンド含む） / 競合の新規・継続・消失 / データ取得日時。

## 4. 定点観測ページ管理 【確定】

正本: `config_v2/pages.yaml`（26ページ、出典は `Page-List - シート1.csv`）。

スキーマ:

```yaml
pages:
  - id: string            # URLパス末尾から機械生成。ハイフン/アンダースコアは元表記のまま保持
    title: string          # 公開ページの<title>/H1で正誤確認済み
    url: string
    group: service | branding
    category: service | definition
    priority: null         # 運用で変わるため固定管理しない。値を使う分析ロジックを書く場合は null 許容が前提
    enabled: true | false   # 現状全ページ true
    objective:              # 分析軸（複数可）。既定は group/category から付与、実態が異なるページは個別上書き
      # service カテゴリ → [conversion]
      # definition カテゴリ → [knowledge]
      # 例外: 大阪ページ（branding-osaka-branding-company）→ [regional, comparison]
```

ルール:

- URLを主キーにする。ページタイトルは表示用で結合キーにしない。
- コードにURLをハードコードしない。取得・分析・レポート表示は本ファイルを参照する。
- URL変更時は旧URL/新URLの対応を残す（現時点では未発生、発生時にフィールド追加を検討）。
- `seo_monitor` / `aio_monitor` フィールドは現時点では不要（`enabled` のみで管理）。将来ページ単位でSEO/AIO監視を分けたくなった場合に再検討する。

### マガジン上位ランディングページ 【確定：pages.yamlに含めない】

`/branding/` 配下で期間中の検索流入が上位のページは、固定リストではなく**レポート生成時にSearch Consoleデータから動的抽出する**。`pages.yaml` には含めない（固定管理と動的抽出を分離する設計判断）。

抽出観点: Click上位 / Impression上位 / 前週比の大幅上昇・下降 / 新規上位入り。可能なら ブランド・ブランディング関連 / 指名検索関連 / 採用・組織関連 / その他 に分類する。

## 5. 施策管理 【確定】

正本: `docs/current-initiatives.md`（**ユーザー管理**。Claude Codeはコード・Gitログ・レポートから値を推測して自動更新・自動確定しない）。

最小項目: 対象（URLまたはページ群） / 状態 / 実装日 / 評価予定日 / メモ。

ルール:

- 実装日が `unknown` の施策は、14日・28日評価の対象に含めない。
- 実装済み施策を新規施策として再提案しない。
- 効果確認待ちの施策は、原則として追加変更ではなく観測を提案する。
- 管理ファイルが読めない/空の場合、Action Itemを自動確定しない。

現在登録済み（雛形）: Definition記事間の内部リンク／DefinitionからWorksへの接続／大阪ページの関連リンク強化。いずれも実装日 `unknown`。

## 6. データソース

### 6.1 Search Console 【確定：直接取得】

Search Console APIから直接取得（既存OAuth2デスクトップ認証を再利用可能。Phase 0で確認済み: `credentials.json` + `token.pickle`）。

必要粒度: `[date]` / `[date, query]` / `[date, page]` / `[date, page, query]`。指標: clicks, impressions, ctr, position。

期間: 最新確定データ日を末日とし、当週7日と直前7日を同条件で比較。可能なら4週・8週推移も保存。最新確定日を自動判定できない場合は `today - 3 days` を候補とし検証する。

サイト合計とページ・クエリ合計の不一致は、Search Consoleの集計特性として記録し、エラーと断定しない（週次比較内では同一条件で取得する）。

### 6.2 AIO観測データ 【確定：既存SerpAPI経路を改修して再利用】

現状: `generate_report.py`内の`fetch_google_aio()`と`aio_daily_monitor.py`にAIO取得ロジックが重複実装されている（Phase 0で確認）。v2では統合し、以下のステータス区分を新規実装する（現状未実装）。

```yaml
acquisition_status: success | not_generated | not_observed | failed | not_run
```

必須ルール:

- `not_run`/`failed`を「引用なし」と扱わない。「未取得」と「観測したが引用なし」を分ける。
- 日次観測と週次スナップショットを混同しない。
- AIO回答本文の変化は、取得成功した同条件の観測間だけで比較する。

保持項目: query, observed_at, provider, aio_generated, tcd_cited, tcd_urls, cited_domains, cited_urls, answer_text_hash, acquisition_status, error_message。

### 6.3 GA4 【確定：Phase 3で統合。Phase 2の対象外】

Phase 0確認事項: 現状GA4は本流パイプライン未統合。`fetch_ga4_referrers.py`という単発スクリプトが独立して存在するのみ（プロパティID `269009748`、日付ハードコード、`token_ga4.pickle`で別トークン管理）。

初期版に含めるか、Phase 2以降に後回しにするかは**ユーザー判断待ち**（本要件定義では確定させない）。含める場合の追加確認事項:

- コンバージョン/inquiry関連イベントの定義
- preview/customize URL等の除外ルールの具体例
- `requirements.txt`への`google-analytics-data`追記

含めない場合、初期版はSearch Console + AIOのみで骨格を固める。

## 7. データ処理

### 7.1 URL正規化（現状未実装、新規構築）

http/https統一、host統一、URLエンコード表現差、末尾スラッシュ、index.html等、不要クエリパラメータ除去、canonical対応、preview/customize URL除外。**ハイフンとアンダースコアは自動的に同一視しない**（実在URL・HTTP応答・canonical・リダイレクトを確認して別URL扱い。`pages.yaml`のid設計にも同じ方針を適用済み）。

### 7.2 原データ保存

`data_v2/raw/gsc/` `data_v2/raw/ga4/`（GA4を含める場合）`data_v2/raw/aio/` にAPIレスポンスを加工前のまま保存し、同一期間のレポートを再現できるようにする。

### 7.3 正規化済みデータ

`data_v2/processed/` に共通正規化データを作る（SQLite/Parquet/JSONいずれか、再現性・テスト容易性・将来のダッシュボード利用を考慮）。レポートと将来のダッシュボードは同じ正規化済みデータを参照する。

## 8. データ検証要件

レポート生成前に検証を実行し、重大エラーがある場合は通常レポートを生成せずデータ品質レポートを出力する。

検証対象: 期間の整合性（今週・前週が各7日、比較期間が連続、最新日が確定データ）／Search Console取得成功・対象プロパティ・空データなし・重複行なし・重点クエリ取得条件が前週と同一／（GA4を含める場合）取得成功・対象プロパティ・preview混入数・conversion定義／AIO各重点クエリの実行状態・成功数・失敗数・未実行数・週次比較可能数／ページ設定のURL重複・group未設定・無効URL・activeページのデータ取得有無。

出力例:

```yaml
data_quality:
  status: pass | warning | fail
  warnings: []
  errors: []
  latest_confirmed_gsc_date: YYYY-MM-DD
  aio_success_rate: 1.0
```

## 9. 分析・判断ルール

閾値は `config_v2/thresholds.yaml`（次工程で作成）で管理し、コードに固定しない。初期値候補: `low_impressions: 50` / `low_clicks: 5` / `position_change_meaningful: 2.0` / `minimum_weeks_for_trend: 3`。

- 少数データの増減率を過大評価しない（例: 2→3クリックを「50%成長」と強調しない）。
- 各主要な示唆にConfidence（High/Medium/Low/Not assessable）を付与する。判断材料: データ量、取得成功率、複数週の継続性、SEOとAIOの一致、ページ単位とクエリ単位の一致。
- 施策判断は「実施／継続観測／効果確認待ち／再調査／判断保留」の5分類。
- Impressionだけで施策を決めない。ページ平均だけでなく主要クエリ別に確認する。
- 順位10位以下ではタイトル変更より検索意図・本文・情報量・内部リンク・Entityを優先。1〜5位でCTRだけが低い場合にタイトル・description改善を検討する。
- 公開・更新後14日で一次評価、28日で再評価する（`docs/current-initiatives.md`の実装日起点。unknownの施策は対象外）。
- 新規記事は既存記事との差分・独立した検索意図・カニバリ・サービス接続を確認してから提案する。

## 10. レポート構成（初期版）

1. 今週の結論（3〜6項目）
2. データ品質・観測条件
3. 重点市場サマリー（4クエリ）
4. 市場・競合分析（市場構造。競合の新規・継続・消失を区別）
5. 定点観測ページ（グループ別: サービス案内系／サービスDefinition系／マガジンDefinition系／マガジン上位ランディングページ）
6. 施策評価（`docs/current-initiatives.md`と突合。実装日unknownは14/28日評価から除外して表示）
7. 次週アクション（最大5件。対象・観測事実・判断・具体的作業・優先度・Confidence・実施／保留理由。競合名を主語にせず、TCDが行う作業を主語にする）
8. 補足データ（主要クエリ・上位LP・データ取得ログ）

不要にセクションを増やさない。ブランド指名クエリ中心の全サイトTOP20は主要セクションに置かない。

## 11. 出力要件

初期版必須: HTML / Markdown / 機械可読な分析結果JSON（`reports_v2/YYYY-MM-DD-weekly-report.{html,md,json}`）。JSON・Markdown・HTMLで主要数値を一致させる。PDF・Webダッシュボードは初期版の必須要件にしない。

## 12. 非要件（初期版で行わない）

Looker Studioからのデータ取得／既存レポート全セクションの再現／全ページ・全クエリの詳細掲載／PDFを主出力にすること／少数データからの自動新規記事提案／AIO未取得時の競合変化断定／AI文章生成だけでAction Itemを確定すること／WordPress本番ファイルの変更／TCDサイト本体への自動反映。

## 13. 現時点でのオープン事項（ユーザー判断待ち）

1. **GA4を初期版に含めるか**（6.3節）。未決定。
2. `pages.yaml`の`priority`が`null`のまま運用する前提で、レポート内の並び順・強調ロジックをどう設計するか（priorityに依存しない表示順が必要）。
3. `docs/current-initiatives.md`の実装日（現在すべて`unknown`）— 判明次第ユーザーが追記。
4. `config_v2/thresholds.yaml`の閾値初期値は仮置き候補であり、運用データを見て調整する前提。

## 14. 実装フェーズ状況

- Phase 0（現状調査）: 完了。
- Phase 1（要件・データ仕様の固定）: 完了。`config_v2/pages.yaml`, `docs/current-initiatives.md`, `docs_v2/requirements.md`, `docs_v2/data-contract.md`, `config_v2/queries.yaml`, `config_v2/thresholds.yaml` すべて作成済み。
- Phase 2（データ取得・検証・正規化）: 完了。GA4は含めず、Search Console + AIOのみを対象に実装。
  - `src_v2/auth.py` — OAuth2認証（既存`credentials.json`/`token.pickle`を再利用、webmasters.readonlyのみ要求）
  - `src_v2/config_loader.py` — `config_v2/*.yaml`の読み込み・整合性チェック
  - `src_v2/fetch_gsc.py` — Search Console raw取得（`data_v2/raw/gsc/`）。API失敗は`GscFetchError`でFail Fast
  - `src_v2/fetch_aio.py` — SerpAPI raw取得（`data_v2/raw/aio/`）。acquisition_status 5分類を取得層で判定
  - `src_v2/validate.py` — raw検証、`data_quality_report`生成。status=failで`DataQualityError`（Fail Fast）
  - `src_v2/normalize.py` — URL正規化（このモジュールのみで実施）・クエリ正規化・重点クエリ完全一致/関連判定・AIO引用ドメイン解決
  - `src_v2/pipeline.py` — raw→validate→normalizedのオーケストレーション。`python3 -m src_v2.pipeline`で実行可能
  - `tests_v2/` — 24件のユニットテスト（normalize/validate）。`python3 -m pytest tests_v2/`
  - 実行確認済み: 週次期間（2026-07-20〜2026-07-26）でdata_quality=pass、`data_v2/processed/{cur_start}_{cur_end}/normalized.json`まで生成
- Phase 3（GA4統合）: 未着手。ユーザー判断によりPhase 2から分離。
- Phase 4（分析: Confidence・施策突合・重点市場集計）: 完了。`src_v2/analysis/`配下に実装（詳細は`data-contract.md`6節）。
  - `confidence.py` / `common.py` / `market.py` / `pages.py` / `initiatives.py` / `landing_pages.py` / `summary.py`
  - 出力: `data_v2/processed/{period}/analysis.json`（Finding配列。以後のレポート生成はこのJSONのみ参照する）
  - **2026-07-27改修**: `market.py`の集計方式を完全一致からAND条件（トークン一致・語順不問）に変更（3節参照）。`evidence`のフィールド名を`current_exact`/`comparison_exact`/`current_related` → `current`/`comparison`/`matched_query_variants`に整理。実データで検証: 「ブランディング会社 東京」がAND条件により9種類の実クエリを捕捉しImpressions 36→96に、「大阪」も4種類捕捉し38に増加。「インナーブランディング会社 東京／大阪」はAND条件でも一致0件のまま（＝本当にデータが存在しないことをLooker全期間検証込みで確認済み）
  - 実行確認済み: 2026-07-20〜2026-07-26でFinding 43件生成（market 4 / competitor 0 / page 26 / initiative 3 / magazine_landing_page 10）。ユニットテスト計29件（`tests_v2/test_analysis.py`、Phase 2分と合わせて計73件）
  - 既知の制約: `weeks_available`は`data_v2/processed/`の蓄積週数に依存するため、運用初期は「継続週数」のConfidence要因が働きにくい（正しくNot assessable/Low寄りになる。過大評価はしていない）
- Phase 5（レポート生成: JSON/Markdown/HTML）: 完了。`src_v2/report/`配下に実装。閾値・判定ロジックは変更していない（`src_v2/analysis/`は無変更。`pages.py`にevidence.title追加、`market.py`にevidence.competitor_summary追加のみ＝いずれも記述的メタデータで判定ロジックではない）。
  - `view_model.py` — `analysis.json`のみを入力とする共通View Model。severity→confidence→categoryで並べ替え、8章構成へ分配する。分析ロジックの追加はしていない（既存Findingの並べ替え・グルーピング・集計・文言の重複除去のみ）
  - レポート構成（8章、ユーザー指定。当初6章→⑦→⑧の順で拡張）:
    1. 今週の結論（Executive Summary） — **Claude Codeがレポート生成のたびにanalysis.jsonを読んで執筆するnarrativeを優先表示**。未指定時は数値ベースの下書き箇条書き（7行）にフォールバック。追加APIキー・課金なし（`build.py --narrative-file`で差し込み）
    2. 市場・競合分析（SEO + AIO） — **主役**。`category in (market, competitor)`を常に全件表示（confidence不問）。各クエリのAIO競合ドメイン数・前週比・新規/消失を`market.py`の`competitor_summary`で常時算出（比較不可の週はcomparison_available=falseを明示、0扱いにしない）
    3. サイト全体サマリー（カテゴリ別） — `pages.yaml`のgroup/categoryでロールアップ（サービス案内系／サービスDefinition系／マガジンDefinition系）
    4. 改善・悪化ページ（TOP5） — 前週比position_deltaの上位5件を改善/悪化それぞれ抽出
    5. 施策評価（14日・28日評価） — 常に全件表示（confidence不問）
    6. 判断保留・データ不足 — `category in (page, competitor)`かつ低confidence/判断保留。④のTOP5に既出のページは重複させない
    7. 今週やること（Priority Action） — `judgment in (実施, 再調査)`のFindingを優先順位順に最大5件抽出するだけ（新しい優先度計算はしない）。0件なら「継続観測中心」と明示
    8. 上位ランディングページ（/branding/） — `src_v2/analysis/landing_pages.py`（新規）が、/branding/配下でpage_id未解決（＝pages.yaml定点観測対象外）かつ`config_v2/excluded_landing_pages.yaml`の除外URLに載っていない候補をクリック数降順でTOP10抽出。除外URL一覧は本文よりやや小さいフォントで併記（何を除外しているか常に見える化）
  - `render_json.py` / `render_md.py` / `render_html.py` — 同一View Modelから3形式を生成。HTMLは`dataviz`スキルの検証済みステータスパレット（good/warning/serious/critical、色はドットのみに乗せテキストは無色）を採用し、ライト/ダーク両対応。CSSカスタムプロパティは`:root`スコープに統一し、`body`背景にも`--page-plane`を適用。ページ背景`--page-plane: #d4d4c3`、②市場カード背景`--market-surface: #efefe5`（ユーザー指定）
  - `build.py` — CLI。`python3 -m src_v2.report.build [--narrative-file PATH]`で`reports_v2/{period}/report.{json,md,html}`を生成
  - 実行確認済み（2026-07-20〜2026-07-26、閾値変更なし、Playwrightでライト/ダーク/モバイル目視確認済み）: 市場4件は全てconfidence=Lowで要確認表示、AIO競合は東京7社／大阪2社（いずれも初回観測のため前週比較なし）、Priority Actionは0件（データ不足のため妥当）、上位ランディングページ10件検出・除外URLは現状0件
  - ユニットテスト29件追加（`tests_v2/test_report.py` + `test_build.py` + `test_analysis.py`のlanding_pages分。Phase 2〜5合計71件）
  - **閾値・判定ロジックの調整は行っていない**（ユーザー指示により初回〜4週目のデータ蓄積後に実施予定）
