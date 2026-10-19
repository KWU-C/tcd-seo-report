# 一覧系ページの canonical・URL整合性の修正記録（2026-10-19）

- テーマ: `~/Documents/tcd-2026-theme`、ブランチ `fix/archive-canonical-url`（origin に push 済み。**main へのマージ・本番デプロイは未実施**）
- コミット: e324a93（canonical・og:url・404・著者ページの301）、8d1ac01（内部リンク）
- ステージング反映: https://sg2026.tcd.jp/ （2026-10-19 14:40 JST）
- ステージングの反映前のファイル: `/home/users/2/tcdadmin/theme_backup_sg2026_20261019_144046_archive-canonical-url/`（公開ディレクトリ外、21ファイル）

## 1. 原因

WordPress のパーマリンク設定では、一覧系のURLは末尾スラッシュなしが正本。スラッシュ付きでアクセスすると、WordPress が301でスラッシュなしへ転送する（`x-redirect-by: WordPress`）。

一方、テーマの `tcd_canonical_url()`（functions.php、2026-06-13 のコミット 155af21 で追加）は、一覧系の canonical に `trailingslashit()` で末尾スラッシュを付けていた。そのため、canonical のURLを開くとスラッシュなしへ301される状態だった。og:url とパンくずの構造化データも同じ値を使っていた。

| 種類 | 修正前（本番） |
|---|---|
| Works の各カテゴリと2ページ目以降、/works | canonical はスラッシュあり、そのURLは301 |
| /branding、/service、/about、/recruit、タグ一覧、著者ページ、各ページ送り | 同じ |
| 記事・事例・固定ページ（.html、/tokyo など）、トップ | 問題なし |
| 404ページ | canonical と og:url を出力していた |
| 旧著者URL（/author/{名前}、/branding/{名前}） | 301が2回（テーマがスラッシュ付きへ転送 → WordPress がスラッシュなしへ転送） |

サイトマップ（Google XML Sitemaps、`/sitemap.xml`）の595件はすべてスラッシュなしで、すべて200。サイトマップとリダイレクト先は一致しており、canonical だけが食い違っていた。

内部リンクは、ヘッダー・フッター・一覧の絞り込みナビなどにスラッシュ付きのリンクが約40か所あり、クリックのたびに301を1回経由していた。

## 2. 修正内容

| ファイル | 変更 |
|---|---|
| functions.php | `tcd_canonical_url()` の一覧系（タクソノミー・投稿タイプ・著者・ページ送り・フォールバック）を `user_trailingslashit()` に変更。404 では空文字を返す。旧著者URLの301の転送先2か所をスラッシュなしに |
| header.php | canonical と og:url は空文字のとき（404）出力しない。Definition 記事のパンくずの「ブランディング」を `/branding` に。ナビのリンク |
| 404.php | 案内リンク5本をスラッシュなしに（表示文字列も同じ） |
| footer.php、archive-*.php、single-*.php、taxonomy-*.php、author.php、page-about-top.php、page-*_entry.php | `/works/` `/service/` `/about/` `/branding/` `/recruit/` `/contact/` と著者一覧（`/branding/author/{名前}/`）へのリンクをスラッシュなしに |

変更していないもの: 記事・事例・固定ページのURL、トップページ（`/`）、CSS・JS、データベース（投稿本文・メニュー）。

## 3. ステージングでの検証結果

47URL（Works・サービス・ブランディング・タグ一覧・About・Recruit・著者ページ・ページ送り・404・記事・事例・固定ページ・トップ、それぞれスラッシュあり／なし）で、修正前後を比較した。

| 項目 | 結果 |
|---|---|
| PHP構文（サーバの PHP 8.3 / 7.4 で `php -l`） | 21ファイルともエラーなし |
| 表示時のPHPエラー | 47ページで0件 |
| canonical | 一覧系33ページで、canonical がスラッシュなしになり、canonical のURLはすべて301なしで200 |
| canonical が変わらなかったページ | トップ、記事・事例・固定ページ（.html、/tokyo、/osaka、/contact、/download、/company-profile）。修正前と同一 |
| 404 | canonical・og:url を出力しない（/no-such-page-xyz、/works/no-such-term、/works/naming/page/2、/branding/author/tanaka/page/2） |
| og:url | すべてのページで canonical と同じ値 |
| 構造化データ | 変わったのは Definition 記事のパンくずの「ブランディング」（`/branding/` → `/branding`）の1か所のみ。ほかの JSON-LD は同一 |
| head のその他の出力 | title・description・robots・og（url以外）・link・script は、全ページで修正前と同一 |
| 旧著者URL | /branding/araki、/author/araki、/author/araki/ とも、301が1回で /branding/author/araki（200）へ |
| スラッシュ付きURL | /works/ など、スラッシュ付きでアクセスした場合は従来どおり WordPress が301でスラッシュなしへ（変更なし） |
| 内部リンク（テーマが出すもの） | 20ページから集めた185URLで、テーマ由来の301・404は0件 |
| PC（1440px） | /works/corporate のヘッダー・フッター・絞り込みナビの49リンクが301なしで200。Works のナビが選択状態。横スクロールなし |
| スマートフォン（375px） | 著者ページの74リンクを確認（データベース由来の1件を除き301なし）。メニューを開いて Works を押すと /works へ直接遷移。404ページの案内リンク7本は200。横スクロールなし |

### テーマ以外が原因の古いリンク（管理画面での本文修正が必要。今回は変更しない）

ステージングで見つかった6件を本番（データベースの正本）で調べ直した。本番で修正が必要なのは3件。

| # | リンク元（本番） | リンク先 | 本番のHTTP | 推奨修正 |
|---|---|---|---|---|
| 1 | /service/corporate_branding.html 本文末の「お問い合わせフォームへ →」 | /contact/ | 301 → /contact（200） | 本文の href を `/contact` に |
| 2 | /recruit/career.html 本文の「エントリーフォームはこちら」 | /career_entry/ | 301 → /career_entry（200） | 本文の href を `/career_entry` に |
| 3 | /recruit/career.html 本文の募集職種リスト | /copy-wrighter-ashiya | 404 | **HTMLコメント（`<!-- -->`）の中にあり、画面に表示されず、リンクとしても機能していない。** 募集終了した「【芦屋本社】コピーライター」の行をコメントアウトしたもの。対応は不要。整理するならコメントごと削除。再募集する場合は、先に募集ページを作ってからリンクを戻す |
| 4 | /recruit（新卒採用カードと絞り込みナビ） | /recruit/new_outline.html | **200** | 本番は問題なし。ステージングのみ、データベースが古く new_outline_ashiya.html との転送関係が逆になっている |
| 5 | 本番: /branding/author/kawauchi（代表的な担当案件）。ステージング: トップ | /works/digital/nobori.html | **200** | 本番は問題なし（本番のトップは /works/corporate/nobori_ltd.html にリンク）。ステージングのみ301 |
| 6 | ステージングのみ: タグ一覧・著者ページ・/service/corporate_branding.html | /branding/definition/communication.html | 301 → integrated-branding.html | 本番の該当ページにはこのリンクがない。ステージングのデータベースが古いだけで対応不要 |

本番の同じ範囲（22ページ）を調べた際に、追加で1件見つかった。

| # | リンク元（本番） | リンク先 | 本番のHTTP | 推奨修正 |
|---|---|---|---|---|
| 7 | /recruit/new_outline.html 本文の「エントリーフォームはこちら」 | /recruit_entry/ | 301 → /recruit_entry（200） | 本文の href を `/recruit_entry` に |

（/branding/author/tanaka/ への301もあったが、これはテーマの「一覧へ」リンクで、今回のブランチで修正済み）

## 3-2. 見た目・操作への影響

- **テンプレートの差分は、リンク先（href）だけ。** 変更行46か所のうち42か所は href の末尾スラッシュの削除のみで、残り4か所は head の canonical・og:url を404で出さないための条件。class、要素の構造、CSS、JS は変更していない（差分を機械的に確認）
- **ステージング（修正後）と本番（修正前）の表示HTMLを、主要20ページで比較した。**
  - ヘッダー：全ページで同一（リンク先の末尾スラッシュを除く）
  - 一覧の絞り込みナビ・本文：要素とclassの構成は同一。文言の違いは、両環境のデータベースの違い（サービス名、タグの並び、投稿本文）によるもの
  - フッター：要素とclassの構成は同一。サービス名とタグの並びの違いは、データベースの違いによるもの
  - 読み込むCSS・JSのファイルと内容は、ステージングと本番で同一（base.css、common.js のハッシュ一致）
- **画面に見える変化は1か所だけ：404ページの案内リンクの表示文字列。** 「https://tcd.jp/works/」→「https://tcd.jp/works」など5本。リンク先と表示を揃えたため。元の表示に戻すことも可能
- ナビの選択状態（Works を開いているときに Works が選択表示になる等）は、URLではなくページの種類で判定しており、変わらない

### 目視確認用URL（ステージング）

| 優先 | 箇所 | URL | 見るところ |
|---|---|---|---|
| 1 | PCヘッダー | https://sg2026.tcd.jp/works/corporate | Works が選択状態。Works・Services・About・Magazine・採用情報・お問い合わせを押して、各ページに直接移る |
| 1 | スマートフォンメニュー | https://sg2026.tcd.jp/branding/author/kawauchi （スマートフォン幅） | メニューを開き、Works・Services・About・Magazine・Recruit・お問い合わせを押して移る |
| 1 | Works一覧・カテゴリ | https://sg2026.tcd.jp/works 、https://sg2026.tcd.jp/works/corporate 、https://sg2026.tcd.jp/works/corporate/page/2 、https://sg2026.tcd.jp/works/product/page/3 、https://sg2026.tcd.jp/works/naming | 絞り込みナビの「TOP」、ページ送り |
| 1 | 事例ページ | https://sg2026.tcd.jp/works/corporate/nitto.html | 絞り込みナビの「TOP」、本文下の「Worksトップへ」 |
| 1 | ブランディング一覧 | https://sg2026.tcd.jp/branding 、https://sg2026.tcd.jp/branding/page/2 、タグ一覧 https://sg2026.tcd.jp/branding/%e4%bc%81%e6%a5%ad%e3%83%96%e3%83%a9%e3%83%b3%e3%83%87%e3%82%a3%e3%83%b3%e3%82%b0 | 絞り込みナビの「All」 |
| 1 | ブランディング記事・著者 | https://sg2026.tcd.jp/branding/definition/inner-branding.html 、https://sg2026.tcd.jp/branding/author/kawauchi | 「All」「一覧へ」（ブランディング一覧・著者一覧） |
| 1 | フッター | https://sg2026.tcd.jp/ （ページ下部） | Works・Services・About・採用情報・Branding Magazine・新着一覧・お問い合わせ |
| 1 | 404ページ | https://sg2026.tcd.jp/no-such-page-xyz 、https://sg2026.tcd.jp/works/no-such-term | 案内リンク5本の表示（末尾スラッシュなしに変わる） |
| 2 | サービス | https://sg2026.tcd.jp/service 、https://sg2026.tcd.jp/service/corporate_branding.html | 絞り込みナビの「TOP」 |
| 2 | About | https://sg2026.tcd.jp/about 、https://sg2026.tcd.jp/about/profile.html | 絞り込みナビの「TOP」 |
| 2 | Recruit | https://sg2026.tcd.jp/recruit 、https://sg2026.tcd.jp/recruit/career.html 、https://sg2026.tcd.jp/recruit_entry 、https://sg2026.tcd.jp/career_entry | 絞り込みナビの「TOP」 |
| 2 | 旧著者URL | https://sg2026.tcd.jp/author/araki | 著者ページ（/branding/author/araki）に1回で移る |

## 4. /about の記録（修正前後のURLと検索データ）

| 項目 | 修正前 | 修正後（ステージングで確認） |
|---|---|---|
| アクセスできるURL | /about（200）。/about/ は301で /about へ | 同じ（変更なし） |
| canonical・og:url | https://tcd.jp/about/ | https://tcd.jp/about |
| サイト内のリンク | /about/（ヘッダー・フッター・About のナビ） | /about |
| サイトマップ | 掲載なし（投稿タイプの一覧は /works・/service・/branding・/recruit も掲載なし） | 同じ |

Search Console では、/about/（スラッシュあり）だけが表示されており、/about は0件。canonical がスラッシュありだったため、Google はスラッシュありを代表URLとして扱っていたと考えられる。

週別（クリック / 表示回数 / 平均掲載順位、/about/）

| 週 | /about/ |
|---|---|
| 08/22〜08/28 | 1 / 162 / 4.8 |
| 08/29〜09/04 | 1 / 186 / 8.5 |
| 09/05〜09/11 | 4 / 232 / 6.1 |
| 09/12〜09/18 | 4 / 175 / 7.7 |
| 09/19〜09/25 | 0 / 180 / 5.4 |
| 09/26〜10/02 | 3 / 230 / 5.6 |
| 10/03〜10/09 | 4 / 220 / 6.0 |
| 10/10〜10/16 | 3 / 205 / 6.1 |

主なクエリ（09/19〜10/16、表示回数 / クリック / 平均掲載順位）: 「tcd」371 / 2 / 9.4、「株式会社tcd」155 / 4 / 1.0、「tcd 芦屋」30 / 0 / 1.0、「株式会社ｔｃｄ」27 / 1 / 1.0、「tcd デザイン」17 / 0 / 1.0、「tcd 会社」17 / 0 / 2.5。

本番反映後に確認すること:
- Search Console で /about と /about/ の表示回数を週ごとに並べ、代表URLが /about に移るか（移行中は両方に分かれることがある）
- 「株式会社tcd」「tcd 芦屋」などの社名クエリで、掲載順位（1位）が下がらないか
- URL検査で /about の「Google が選択した正規URL」を確認する（任意）

## 5. 別件: 存在しないフィードへのリンク（今回は修正しない）

各ページの head に WordPress が出力する RSS フィードの `<link rel="alternate">` のうち、一部が404または301になっている（2026-10-19 本番）。

| ページ | 出力されるフィードURL | 結果 |
|---|---|---|
| /works | /works/feed/ | 404（/works/feed も404） |
| /branding | /branding/feed/ | 404（/branding/feed も404） |
| /service | /service/feed/ | 301 → /service/feed（200） |
| /about | /about/feed/ | 301 → /about/feed（200） |
| /recruit | /recruit/feed/ | 301 → /recruit/feed（200） |
| /works/corporate、タグ一覧、著者ページ | …/feed | 200 |

- /works と /branding のフィードは、URLの形に関係なく存在しない。この2つの投稿タイプはテーマの functions.php では登録されておらず（service・about・recruit はテーマで登録）、プラグイン側の登録またはテーマの一覧の上書き処理（functions.php の pre_get_posts）が関係している可能性がある。未確認
- service・about・recruit は、WordPress が投稿タイプの一覧のフィードURLを末尾スラッシュ付きで出力しているため301になる
- 対応案: /works と /branding では、存在しないフィードへの link を出さないようにする（例: 投稿タイプ一覧の `feed_links_extra` を止める）。フィードを使っている外部サービスの有無を確認してから判断する

## 6. 本番反映の手順（確認後にユーザーが実施）

1. https://github.com/KWU-C/tcd-2026-theme/pull/new/fix/archive-canonical-url からプルリクエストを作成・確認・マージ
2. GitHub Actions「Deploy to Production」を実行
3. 本番で確認: 一覧系の canonical がスラッシュなしで200、404 で canonical が出ない、/author/araki が301を1回で著者ページへ、ヘッダー・フッターのリンクが301しない
4. `docs/current-initiatives.md` に施策として記録（実装日はユーザーが記入）。東京LPの施策の評価期間と重なることを明記する
5. 4章の /about の確認を、反映後2〜4週で行う

戻す場合: main で2つのコミットを revert して再デプロイする。ステージングは上記のバックアップから戻せる。
