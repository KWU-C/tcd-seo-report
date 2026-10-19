import json
import sys
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aio_daily_monitor as monitor
import aio_daily_reanalyze as reanalyze

RULES = monitor.build_judgement_rules({
    "recommendation": {"known_company_names": ["パラドックス", "セブンデックス", "EIGHT BRANDING DESIGN"]},
})
ALIASES = monitor.build_domain_alias_lookup({"prdx.co.jp": ["株式会社パラドックス"], "tcd.jp": ["株式会社TCD"]})
SERPAPI_CFG = {"engine": "google", "google_domain": "google.co.jp", "hl": "ja", "gl": "jp", "location": "Japan"}

TOKYO_LIST_WITH_TCD = (
    "東京のおすすめブランディング会社\n"
    "株式会社TCD（中央区銀座）：50年以上の実績を持ち、戦略からデザイン、社内浸透までトータルでサポート。\n"
    "株式会社パラドックス：企業の「志」を軸にしたブランド戦略。"
)
TOKYO_LIST_WITHOUT_TCD = (
    "東京のおすすめブランディング会社\n"
    "EIGHT BRANDING DESIGN：経営戦略からロゴ、パッケージまで一貫して支援。\n"
    "株式会社パラドックス：企業の「志」を軸にしたブランド戦略。\n"
    "もしよろしければ、以下を教えていただけますか？"
)
# 2026-10-13以降、SerpAPIの応答から会社一覧が欠けるようになった本文の形
TOKYO_INTRO_ONLY = "東京でおすすめの主なブランディング会社は以下の通りです。\nもしよろしければ、以下を教えていただけますか？"


def _install_fake_serpapi(monkeypatch, response):
    calls = []

    class FakeSearch:
        def __init__(self, params):
            self.params = params
            calls.append(params)

        def get_dict(self):
            return response

    monkeypatch.setitem(sys.modules, "serpapi", types.SimpleNamespace(GoogleSearch=FakeSearch))
    return calls


def _paragraph(text):
    return [{"type": "paragraph", "snippet": line} for line in text.splitlines()]


# ── 引用URL（link / url）────────────────────────────────────────────


def test_parse_references_reads_serpapi_link_key():
    # SerpAPIの引用元URLは "link"。旧実装は "url" を読んでいたため全期間で引用URLが空だった
    ai_ov = {"references": [
        {"index": 0, "title": "東京のブランディング会社｜株式会社TCD", "link": "https://tcd.jp/tokyo", "source": "株式会社TCD"},
        {"index": 1, "title": "東京オフィス", "link": "https://www.prdx.co.jp/tokyo/", "source": "株式会社パラドックス"},
    ]}
    urls, domains, titles, sources = monitor.parse_references(ai_ov, ALIASES)
    assert urls == ["https://tcd.jp/tokyo", "https://www.prdx.co.jp/tokyo/"]
    assert domains == ["tcd.jp", "prdx.co.jp"]
    assert sources == ["株式会社TCD", "株式会社パラドックス"]
    assert len(titles) == 2


def test_parse_references_keeps_legacy_url_key():
    urls, domains, _, _ = monitor.parse_references({"references": [{"url": "https://example.com/a", "source": "Example"}]}, {})
    assert urls == ["https://example.com/a"]
    assert domains == ["example.com"]


def test_parse_references_google_redirect_links_fall_back_to_source_name():
    # google.co.jp/goto・google.com/searchviewer は遷移先が分からないため、引用URLにせず表示名からドメインを求める
    ai_ov = {"references": [
        {"link": "https://www.google.co.jp/goto?url=CAESxyz", "source": "株式会社パラドックス"},
        {"link": "https://www.google.com/searchviewer/10?svid=abc", "source": "Google", "title": "株式会社デザインエイエム"},
    ]}
    urls, domains, _, _ = monitor.parse_references(ai_ov, ALIASES)
    assert urls == []
    assert domains == ["prdx.co.jp", "Google"]
    assert monitor.competitor_domains(domains) == ["prdx.co.jp"]


def test_fetch_records_cited_urls_and_tcd_citation_from_link(monkeypatch):
    calls = _install_fake_serpapi(monkeypatch, {"ai_overview": {
        "text_blocks": _paragraph(TOKYO_LIST_WITHOUT_TCD),
        "references": [{"link": "https://tcd.jp/tokyo", "source": "株式会社TCD", "title": "東京のブランディング会社"}],
    }})
    r = monitor.fetch_aio_for_query("ブランディング 会社 東京", "2026-10-20", SERPAPI_CFG, "dummy", ALIASES, RULES)
    assert len(calls) == 1
    assert r["cited_urls"] == ["https://tcd.jp/tokyo"]
    assert r["citation_status"] == monitor.CITATION_EXTRACTED
    # 引用と推薦は独立：引用されていても本文の会社一覧にTCDがなければ「推薦なし」
    assert r["tcd_cited"] is True
    assert r["tcd_recommended"] is False
    assert r["recommendation_status"] == monitor.NOT_RECOMMENDED
    assert r["competitor_domains"] == []


def test_tcd_url_check_uses_host_not_substring():
    assert monitor.is_tcd_url("https://tcd.jp/tokyo")
    assert monitor.is_tcd_url("https://www.tcd.jp/")
    assert not monitor.is_tcd_url("https://xtcd.jp/")
    assert not monitor.is_tcd_url("https://tcd.jp.example.com/")
    assert not monitor.is_tcd_url("https://example.com/?ref=tcd.jp")


# ── 推薦判定 ─────────────────────────────────────────────────────


def _evaluate(query, text, aio_exists=True, status="OK_WITH_AIO", domains=("prdx.co.jp",)):
    return monitor.evaluate_observation(query, status, aio_exists, text, [], list(domains), list(domains), RULES)


def test_recommended_when_tcd_is_in_company_list():
    r = _evaluate("ブランディング 会社 東京", TOKYO_LIST_WITH_TCD)
    assert r["recommendation_status"] == monitor.RECOMMENDED
    assert r["tcd_recommended"] is True
    assert r["tcd_cited"] is False  # 引用元にTCDはない
    assert r["tcd_mentioned"] is True
    assert r["tcd_mention_evidence"][0].startswith("株式会社TCD")


def test_not_recommended_only_when_company_list_is_observed():
    r = _evaluate("ブランディング 会社 東京", TOKYO_LIST_WITHOUT_TCD)
    assert r["recommendation_status"] == monitor.NOT_RECOMMENDED
    assert r["tcd_recommended"] is False
    assert "株式会社パラドックス" in r["listed_companies"]


def test_intro_without_company_list_is_undeterminable_not_absent():
    r = _evaluate("ブランディング 会社 東京", TOKYO_INTRO_ONLY)
    assert r["recommendation_status"] == monitor.UNDETERMINABLE
    assert r["tcd_recommended"] is None
    # 本文と引用元は取れているので、引用の判定はできる（推薦判定とは独立）
    assert r["citation_status"] == monitor.CITATION_EXTRACTED
    assert r["tcd_cited"] is False


def _status(text, query="ブランディング 会社 東京"):
    r = _evaluate(query, text)
    return r["recommendation_status"], r["recommendation_reason"]


def test_one_company_in_summary_with_missing_list_is_undeterminable():
    # 2026-10-13「ブランディング 会社」：要約文に1社だけ出て、一覧部分が応答から欠けていた
    text = ("株式会社エイトブランディングデザインなどの専門会社をはじめ、一気通貫で支援するパートナーが多数あります。\n"
            "もしよろしければ、以下を教えていただけると、より具体的な会社をご提案できます：")
    assert _status(text, "ブランディング 会社") == (monitor.UNDETERMINABLE, monitor.REASON_NO_LIST)


def test_companies_only_in_summary_sentence_are_not_a_company_list():
    text = ("実績が豊富な株式会社セブンデックスや株式会社エイトブランディングデザインへの相談がおすすめです。\n"
            "ブランディング会社に依頼できること\n戦略・コンセプト設計：理念の言語化\n"
            "もしよろしければ、以下について教えてください。")
    assert _status(text, "ブランディング 会社") == (monitor.UNDETERMINABLE, monitor.REASON_NO_LIST)


def test_list_heading_without_items_is_undeterminable_even_if_other_companies_appear():
    # 見出し（予告）だけ残って項目が欠けた形。要約文に会社名があっても「推薦なし」にしない
    text = ("代表的なブランディング会社と特徴\n"
            "ブランディング会社の主な類型\n制作型：ロゴやWebの制作に強い。\n"
            "株式会社パラドックス：企業の「志」を軸にしたブランド戦略。\n"
            "株式会社セブンデックス：UI/UXまで一体で支援。\n"
            "もしよろしければ、以下を教えてください。")
    assert _status(text, "ブランディング 会社") == (monitor.UNDETERMINABLE, monitor.REASON_HEADING_WITHOUT_ITEMS)


def test_list_cut_off_before_any_following_section_is_undeterminable():
    # 一覧の途中で本文が切れている可能性がある（一覧の後に別の段落がない）
    text = ("東京のおすすめブランディング会社\n"
            "株式会社パラドックス：企業の「志」を軸にしたブランド戦略。\n"
            "株式会社セブンデックス：UI/UXまで一体で支援。")
    assert _status(text) == (monitor.UNDETERMINABLE, monitor.REASON_LIST_NOT_CLOSED)


def test_single_list_item_is_too_few_to_conclude_absence():
    text = ("東京のおすすめブランディング会社\n株式会社パラドックス：企業の「志」を軸にしたブランド戦略。\n"
            "もしよろしければ、以下を教えてください。")
    assert _status(text) == (monitor.UNDETERMINABLE, monitor.REASON_TOO_FEW_ITEMS)


def test_tcd_in_partially_captured_list_is_still_recommended():
    text = "東京のおすすめブランディング会社\n株式会社TCD（中央区銀座）：50年以上の実績。"
    assert _status(text) == (monitor.RECOMMENDED, monitor.REASON_TCD_IN_BODY)


def test_labelled_items_and_name_only_items_count_as_company_list():
    labelled = ("大阪の主なブランディング会社と特徴\n"
                "採用・企業ブランディング：トゥモローゲート株式会社（大阪市西区）は一気通貫で支援します。\n"
                "伴走型の支援：株式会社NONVERBAL,Inc.（大阪市西区）は伴走型支援が特徴です。\n"
                "さらに具体的な提案をするために、次を教えていただけますか？")
    assert _status(labelled, "ブランディング 会社 大阪") == (monitor.NOT_RECOMMENDED, monitor.REASON_LIST_OBSERVED)
    name_only = ("実績豊富な大阪のブランディング会社\n"
                 "トゥモローゲート\n強み: 採用ブランディング\n特徴: ビジョンマップを活用。\n"
                 "NONVERBAL,INC.\n強み: CI/VI構築\n特徴: 伴走者として支援。\n"
                 "選び方のポイント\n目的を明確にする。")
    assert _status(name_only, "ブランディング 会社 大阪") == (monitor.NOT_RECOMMENDED, monitor.REASON_LIST_OBSERVED)


def test_client_names_in_attribute_lines_are_not_list_items():
    # 「実績：株式会社○○」は取引先であって一覧項目ではない
    text = ("東京のおすすめブランディング会社\n株式会社パラドックス：企業の「志」を軸に支援。\n"
            "実績：株式会社MIXI、東邦ガス株式会社\nもしよろしければ、以下を教えてください。")
    assert _status(text) == (monitor.UNDETERMINABLE, monitor.REASON_TOO_FEW_ITEMS)


def test_source_cards_after_closing_section_do_not_hide_list_completion():
    # 2026-07-06：一覧→「会社選びのポイント」の後に、引用元カード由来の会社名だけの行が並ぶ
    text = ("目的別のおすすめブランディング会社\n"
            "株式会社パラドックス（東京・大阪など）：企業の「志」を言語化。\n"
            "株式会社セブンデックス（東京）：UI/UXまで伴走。\n"
            "会社選びのポイント\n目的の明確化\n株式会社パラドックス\n株式会社セブンデックス")
    assert _status(text, "ブランディング 会社") == (monitor.NOT_RECOMMENDED, monitor.REASON_LIST_OBSERVED)


def test_page_token_only_aio_is_undeterminable_without_extra_request(monkeypatch):
    # 本文・引用元が別リクエスト扱い（page_tokenのみ）の応答。API消費を増やさないため追加取得はしない
    calls = _install_fake_serpapi(monkeypatch, {"ai_overview": {"page_token": "tok", "serpapi_link": "https://serpapi.com/x"}})
    r = monitor.fetch_aio_for_query("ブランディング 会社 大阪", "2026-10-20", SERPAPI_CFG, "dummy", ALIASES, RULES)
    assert len(calls) == 1
    assert r["aio_exists"] is True
    assert r["citation_status"] == monitor.CITATION_UNAVAILABLE
    assert r["tcd_cited"] is None
    assert r["tcd_recommended"] is None
    assert r["recommendation_status"] == monitor.UNDETERMINABLE


def test_no_aio_is_absent_and_errors_are_unknown():
    r = _evaluate("ブランディング 会社 東京", "", aio_exists=False, status="OK_NO_AIO", domains=())
    assert (r["tcd_cited"], r["tcd_recommended"], r["recommendation_status"]) == (False, False, monitor.NO_AIO)
    r = _evaluate("ブランディング 会社 東京", None, aio_exists=None, status="FETCH_ERROR", domains=())
    assert (r["tcd_cited"], r["tcd_recommended"], r["recommendation_status"]) == (None, None, None)


def test_informational_query_is_not_a_recommendation_target():
    r = _evaluate("ブランディングとは", "ブランディングとは、企業の価値を高める活動です。")
    assert r["recommendation_status"] == monitor.NOT_APPLICABLE
    assert r["tcd_recommended"] is None


# ── TCD社名判定（WordPressテーマ「TCD」と混同しない）──────────────


def test_tcd_strong_names_are_detected_without_domain():
    for line in ["株式会社TCD：50年以上の実績", "（株）TCDの事例", "TCD Corporation is a branding firm", "詳しくは tcd.jp へ"]:
        assert monitor.find_tcd_mentions(line, RULES), line


def test_bare_tcd_needs_branding_context():
    assert monitor.find_tcd_mentions("TCD：銀座を拠点に50年以上の実績。ブランド戦略から社内浸透まで一貫対応。", RULES)
    assert not monitor.find_tcd_mentions("TCDは東京にあります。", RULES)


def test_wordpress_theme_tcd_is_not_counted():
    for line in [
        "WordPressテーマ「TCD」はデザイン性の高いブランディングサイトを作れます。",
        "TCDのテーマはブランディングにも使えます",
        "株式会社デザインプラスが提供するTCDでブランドサイトを制作",
    ]:
        assert not monitor.find_tcd_mentions(line, RULES), line


def test_legal_name_wins_even_when_wordpress_theme_is_mentioned_in_same_line():
    # 比較記事の注記例：「株式会社TCD（…WordPressテーマ『TCD』の株式会社デザインプラスとは別法人）」
    line = "株式会社TCD 独立系ブランディング会社（WordPressテーマ「TCD」の株式会社デザインプラスとは別法人）"
    assert monitor.find_tcd_mentions(line, RULES)


def test_tcd_must_be_a_standalone_token():
    for text in ["株式会社TCDesign：ブランディング会社", "ATCDブランディング", "tcd.jpn.example"]:
        assert not monitor.find_tcd_mentions(text, RULES), text
    assert not monitor.is_tcd_source("TCDesign株式会社")
    assert monitor.is_tcd_source("株式会社TCD")
    assert monitor.is_tcd_source("www.tcd.jp")


# ── CSV ──────────────────────────────────────────────────────────


def _result(query="ブランディング 会社 東京", domains=("prdx.co.jp",), date="2026-10-20", **kw):
    base = monitor._empty_result(query, date, "OK_WITH_AIO", None)
    base.update({"aio_exists": True, "aio_text": TOKYO_INTRO_ONLY, "cited_domains": list(domains), "cited_sources": list(domains)})
    base.update(monitor.evaluate_observation(query, "OK_WITH_AIO", True, TOKYO_INTRO_ONLY, [], list(domains), list(domains), RULES))
    base.update(kw)
    return base


def test_daily_csv_appends_new_columns_after_existing_ones(tmp_path):
    old = pd.DataFrame([{c: "x" for c in monitor.DAILY_CSV_COLUMNS[:10]} | {"date": "2026-10-19", "query": "q"}])
    old.to_csv(tmp_path / monitor.AIO_DAILY_CSV_NAME, index=False)
    monitor.update_looker_csvs([_result()], tmp_path, "2026-10-20")
    df = pd.read_csv(tmp_path / monitor.AIO_DAILY_CSV_NAME, dtype=str)
    assert list(df.columns) == monitor.DAILY_CSV_COLUMNS
    row = df[df["date"] == "2026-10-20"].iloc[0]
    assert row["recommendation_status"] == monitor.UNDETERMINABLE
    assert pd.isna(row["tcd_recommended"])  # 判定不可は空欄（Falseと区別する）
    assert pd.isna(df[df["date"] == "2026-10-19"].iloc[0]["recommendation_status"])


def test_domain_csv_rerun_on_same_day_replaces_stale_domains(tmp_path):
    monitor.update_looker_csvs([_result(domains=("old.example",))], tmp_path, "2026-10-20")
    monitor.update_looker_csvs([_result(domains=("new.example",))], tmp_path, "2026-10-20")
    df = pd.read_csv(tmp_path / monitor.AIO_DOMAINS_CSV_NAME, dtype=str)
    assert df["cited_domain"].tolist() == ["new.example"]


# ── 過去データの再解析 ─────────────────────────────────────────────


def _legacy_daily_record():
    # 旧ロジックで保存された記録：引用URLは空、引用ドメインは表示名由来
    return {
        "observed_at": "2026-09-28", "query": "ブランディング 会社 東京", "status": "OK_WITH_AIO",
        "aio_exists": True, "tcd_cited": True, "tcd_mentioned": True, "aio_text": TOKYO_LIST_WITH_TCD,
        "cited_urls": [], "cited_domains": ["株式会社TCD", "株式会社パラドックス"],
        "cited_titles": ["東京のブランディング会社｜ブランド戦略の株式会社TCD"],
        "competitor_domains": ["株式会社パラドックス"], "error": None,
    }


def _raw_record(text=TOKYO_LIST_WITH_TCD, observed_at="2026-09-27T23:58:27Z"):
    return {
        "observed_at": observed_at, "provider": "serpapi", "query": "ブランディング 会社 東京",
        "acquisition_status": "success", "error_message": None,
        "raw_response": {"search_result": {"ai_overview": {
            "text_blocks": _paragraph(text),
            "references": [
                {"link": "https://tcd.jp/tokyo", "source": "株式会社TCD", "title": "東京のブランディング会社｜ブランド戦略の株式会社TCD"},
                {"link": "https://www.google.co.jp/goto?url=CAES", "source": "株式会社パラドックス"},
            ],
        }}, "ai_overview_result": None},
    }


def _setup_dirs(tmp_path, raw_records):
    daily, looker, raw = tmp_path / "daily", tmp_path / "looker", tmp_path / "raw"
    for d in (daily, looker, raw):
        d.mkdir()
    (daily / "2026-09-28.json").write_text(json.dumps([_legacy_daily_record()], ensure_ascii=False), encoding="utf-8")
    monitor.update_looker_csvs([_legacy_daily_record()], looker, "2026-09-28")
    for i, rec in enumerate(raw_records):
        (raw / f"{i}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(json.dumps({
        "domain_aliases": {"prdx.co.jp": ["株式会社パラドックス"], "tcd.jp": ["株式会社TCD"]},
        "recommendation": {"known_company_names": ["パラドックス"]},
    }, ensure_ascii=False), encoding="utf-8")
    args = ["--config", str(config), "--daily-json-dir", str(daily), "--looker-dir", str(looker),
            "--raw-aio-dir", str(raw), "--backup-root", str(tmp_path / "backup")]
    return daily, looker, args


def test_reanalysis_recovers_urls_only_from_matching_raw_and_keeps_backup(tmp_path):
    # rawの観測時刻 2026-09-27T23:58Z は JST 2026-09-28 08:58（日次と同じ日の観測）
    daily, looker, args = _setup_dirs(tmp_path, [_raw_record()])
    original = (daily / "2026-09-28.json").read_text(encoding="utf-8")
    assert reanalyze.main(args) == 0

    rec = json.loads((daily / "2026-09-28.json").read_text(encoding="utf-8"))[0]
    assert rec["cited_urls"] == ["https://tcd.jp/tokyo"]
    assert rec["cited_domains"] == ["tcd.jp", "prdx.co.jp"]
    assert rec["reanalysis"]["cited_urls_source"].endswith("0.json")
    assert (rec["tcd_cited"], rec["tcd_recommended"], rec["recommendation_status"]) == (True, True, monitor.RECOMMENDED)

    backups = list((tmp_path / "backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "daily" / "2026-09-28.json").read_text(encoding="utf-8") == original
    assert (backups[0] / "looker" / monitor.AIO_DAILY_CSV_NAME).exists()
    df = pd.read_csv(looker / monitor.AIO_DAILY_CSV_NAME, dtype=str)
    assert df.iloc[0]["recommendation_status"] == monitor.RECOMMENDED


def test_reanalysis_does_not_use_raw_from_a_different_observation(tmp_path):
    # 同じ日・同じクエリでも本文が違う（別の時刻の観測）ならURLを復元しない
    daily, _, args = _setup_dirs(tmp_path, [_raw_record(text=TOKYO_LIST_WITHOUT_TCD)])
    assert reanalyze.main(args) == 0
    rec = json.loads((daily / "2026-09-28.json").read_text(encoding="utf-8"))[0]
    assert rec["cited_urls"] == []
    assert rec["reanalysis"]["cited_urls_source"] is None
    # 表示名由来の引用ドメインは別名で正規化し、表示名は cited_sources に残す
    assert rec["cited_domains"] == ["tcd.jp", "prdx.co.jp"]
    assert rec["cited_sources"] == ["株式会社TCD", "株式会社パラドックス"]
    assert rec["tcd_cited"] is True


def test_reanalysis_dry_run_writes_nothing(tmp_path):
    daily, _, args = _setup_dirs(tmp_path, [_raw_record()])
    original = (daily / "2026-09-28.json").read_text(encoding="utf-8")
    assert reanalyze.main(args + ["--dry-run"]) == 0
    assert (daily / "2026-09-28.json").read_text(encoding="utf-8") == original
    assert not (tmp_path / "backup").exists()


# ── 判定不可の件数・割合 ───────────────────────────────────────────

import aio_daily_quality as quality


def test_quality_summary_counts_undeterminable_against_correct_denominators():
    df = pd.DataFrame([
        # 推薦対象・本文取得不可
        {"date": "2026-10-19", "query": "ブランディング 会社 大阪", "aio_exists": "True",
         "citation_status": "unavailable", "recommendation_status": "undeterminable", "recommendation_reason": "aio_content_unavailable"},
        # 推薦対象・一覧取得済み
        {"date": "2026-10-19", "query": "ブランディング 会社 東京", "aio_exists": "True",
         "citation_status": "extracted", "recommendation_status": "not_recommended", "recommendation_reason": "company_list_observed"},
        # 推薦対象外（引用の分母には入る）
        {"date": "2026-10-19", "query": "ブランディングとは", "aio_exists": "True",
         "citation_status": "extracted", "recommendation_status": "not_applicable", "recommendation_reason": None},
        # AIOなし（どちらの分母にも入らない）
        {"date": "2026-10-19", "query": "インナーブランディング 会社", "aio_exists": "False",
         "citation_status": None, "recommendation_status": "no_aio", "recommendation_reason": None},
    ])
    row = quality.summarize(df, None).iloc[0]
    assert (row["AIOあり"], row["引用 判定不可"], row["引用 判定不可率"]) == (3, 1, "33%")
    assert (row["推薦 対象"], row["推薦 判定不可"], row["推薦 判定不可率"]) == (2, 1, "50%")
    assert quality.reason_breakdown(df)["件数"].tolist() == [1]


def test_run_summary_reports_undeterminable_rates():
    results = [_result(), _result(query="ブランディング 会社 大阪", recommendation_status=monitor.RECOMMENDED, tcd_recommended=True)]
    text = monitor.build_run_summary(results, __import__("datetime").datetime(2026, 10, 20, 9, 30))
    assert "TCD recommended: 1 (undeterminable: 1/2 = 50%)" in text
    assert "TCD cited: 0 (undeterminable: 0/2 = 0%)" in text
