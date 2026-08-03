import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2.analysis import confidence, common, market, pages, initiatives, landing_pages

THRESHOLDS = {
    "low_data": {
        "low_impressions": 50, "low_clicks": 5,
        "position_change_meaningful": 2.0, "minimum_weeks_for_trend": 3,
    },
    "initiative_review": {"day14_after_implementation": 14, "day28_after_implementation": 28},
}


# ── confidence.py ──────────────────────────────────────────────

def test_assess_confidence_no_data_is_not_assessable():
    result = confidence.assess_confidence({"impressions": None, "clicks": None}, THRESHOLDS)
    assert result["level"] == "Not assessable"


def test_assess_confidence_high_when_all_factors_positive():
    result = confidence.assess_confidence({
        "impressions": 500, "clicks": 20, "weeks_available": 4,
        "seo_aio_agree": True, "page_query_agree": True,
    }, THRESHOLDS)
    assert result["level"] == "High"


def test_assess_confidence_low_data_lowers_level():
    result = confidence.assess_confidence({
        "impressions": 5, "clicks": 1, "weeks_available": 1,
    }, THRESHOLDS)
    assert result["level"] in ("Low", "Medium")
    assert any("データ量" in r for r in result["reasons"])


# ── common.py ──────────────────────────────────────────────────

def test_make_finding_requires_valid_severity():
    import pytest
    with pytest.raises(ValueError):
        common.make_finding("id1", "market", "urgent", "High", "msg", "rec")


def test_sum_metrics_weighted_position():
    rows = [
        {"clicks": 2, "impressions": 100, "position": 3.0},
        {"clicks": 0, "impressions": 100, "position": 5.0},
    ]
    result = common.sum_metrics(rows)
    assert result["clicks"] == 2
    assert result["impressions"] == 200
    assert result["position"] == 4.0


def test_group_metrics_groups_by_key():
    rows = [
        {"page_id": "a", "clicks": 1, "impressions": 10, "position": 1.0},
        {"page_id": "a", "clicks": 1, "impressions": 10, "position": 3.0},
        {"page_id": "b", "clicks": 0, "impressions": 5, "position": 8.0},
    ]
    grouped = common.group_metrics(rows, key_fn=lambda r: r["page_id"])
    assert grouped["a"]["clicks"] == 2
    assert grouped["b"]["clicks"] == 0


# ── market.py ──────────────────────────────────────────────────

def _bundle(query_rows_cur, query_rows_prv, page_query_rows_cur=None, aio_observations=None):
    return {
        "period": {"cur_start": "2026-07-20", "cur_end": "2026-07-26", "prv_start": "2026-07-13", "prv_end": "2026-07-19"},
        "gsc": {
            "current": {"gsc_query_daily": query_rows_cur, "gsc_page_query_daily": page_query_rows_cur or []},
            "comparison": {"gsc_query_daily": query_rows_prv, "gsc_page_query_daily": []},
        },
        "aio_observations": aio_observations or [],
    }


QUERIES_CFG = {
    "markets": {
        "branding_tokyo": {
            "query": "ブランディング 会社 東京", "market": "branding", "region": "tokyo",
            "all": ["ブランディング", "会社", "東京"],
        },
    },
}


def test_analyze_market_low_data_is_hold_judgment():
    bundle = _bundle(
        query_rows_cur=[{"query": "ブランディング 会社 東京", "clicks": 0, "impressions": 5, "ctr": 0.0, "position": 20.0}],
        query_rows_prv=[],
    )
    findings = market.analyze_market(bundle, None, QUERIES_CFG, THRESHOLDS)
    main = [f for f in findings if f["category"] == "market"][0]
    assert main["judgment"] == "判断保留"


def test_analyze_market_position_over_10_recommends_content_review():
    bundle = _bundle(
        query_rows_cur=[{"query": "ブランディング 会社 東京", "clicks": 10, "impressions": 300, "ctr": 0.03, "position": 15.0}],
        query_rows_prv=[{"query": "ブランディング 会社 東京", "clicks": 10, "impressions": 300, "ctr": 0.03, "position": 15.0}],
    )
    findings = market.analyze_market(bundle, None, QUERIES_CFG, THRESHOLDS)
    main = [f for f in findings if f["category"] == "market"][0]
    assert main["judgment"] == "再調査"
    assert "ページ×クエリ" in main["recommendation"]


def test_analyze_market_position_drop_is_warning():
    bundle = _bundle(
        query_rows_cur=[{"query": "ブランディング 会社 東京", "clicks": 10, "impressions": 300, "ctr": 0.03, "position": 8.0}],
        query_rows_prv=[{"query": "ブランディング 会社 東京", "clicks": 10, "impressions": 300, "ctr": 0.03, "position": 5.0}],
    )
    findings = market.analyze_market(bundle, None, QUERIES_CFG, THRESHOLDS)
    main = [f for f in findings if f["category"] == "market"][0]
    assert main["severity"] == "warning"
    assert main["judgment"] == "再調査"


def test_analyze_market_and_matching_aggregates_word_order_variants():
    # 2026-07-27方針転換: 語順違い（東京 ブランディング 会社 おすすめ）も
    # 全トークンを含むためAND一致として合算する（完全一致だけに絞らない）
    bundle = _bundle(
        query_rows_cur=[
            {"query": "ブランディング 会社 東京", "clicks": 5, "impressions": 200, "ctr": 0.025, "position": 4.0},
            {"query": "東京 ブランディング 会社 おすすめ", "clicks": 2, "impressions": 50, "ctr": 0.04, "position": 6.0},
        ],
        query_rows_prv=[{"query": "ブランディング 会社 東京", "clicks": 5, "impressions": 200, "ctr": 0.025, "position": 4.0}],
    )
    findings = market.analyze_market(bundle, None, QUERIES_CFG, THRESHOLDS)
    main = [f for f in findings if f["category"] == "market"][0]
    assert main["evidence"]["current"]["clicks"] == 7
    variants = {v["query"]: v["clicks"] for v in main["evidence"]["matched_query_variants"]}
    assert variants == {"ブランディング 会社 東京": 5, "東京 ブランディング 会社 おすすめ": 2}


def test_analyze_market_partial_token_query_excluded():
    # 「インナーブランディング 支援」のようにtokenの一部しか含まないクエリは集計に含めない
    bundle = _bundle(
        query_rows_cur=[
            {"query": "ブランディング 会社 東京", "clicks": 5, "impressions": 200, "ctr": 0.025, "position": 4.0},
            {"query": "ブランディング 支援", "clicks": 100, "impressions": 1000, "ctr": 0.1, "position": 1.0},
        ],
        query_rows_prv=[],
    )
    findings = market.analyze_market(bundle, None, QUERIES_CFG, THRESHOLDS)
    main = [f for f in findings if f["category"] == "market"][0]
    assert main["evidence"]["current"]["clicks"] == 5


def test_competitor_summary_computes_new_and_dropped():
    aio_cur = {"query": "ブランディング 会社 東京", "acquisition_status": "success", "cited_domains": ["paddledesign.co.jp", "new-competitor.com"]}
    aio_prv = {"query": "ブランディング 会社 東京", "acquisition_status": "success", "cited_domains": ["paddledesign.co.jp", "dropped-competitor.com"]}
    summary = market._competitor_summary(aio_cur, aio_prv)
    assert summary["comparison_available"] is True
    assert summary["current_count"] == 2
    assert summary["previous_count"] == 2
    assert summary["new_domains"] == ["new-competitor.com"]
    assert summary["dropped_domains"] == ["dropped-competitor.com"]


def test_competitor_summary_not_comparable_without_previous_data():
    aio_cur = {"query": "q", "acquisition_status": "success", "cited_domains": ["x.com"]}
    summary = market._competitor_summary(aio_cur, None)
    assert summary["comparison_available"] is False
    assert summary["current_count"] == 1
    assert summary["previous_count"] is None


def test_competitor_findings_single_occurrence_is_info_not_warning():
    """1回だけの新規引用は継続性未確認としてseverity=infoに倒す（単週変化を参入と断定しない）。"""
    aio_cur = {"query": "ブランディング 会社 東京", "acquisition_status": "success", "cited_domains": ["paddledesign.co.jp", "new-competitor.com"]}
    aio_prv = {"query": "ブランディング 会社 東京", "acquisition_status": "success", "cited_domains": ["paddledesign.co.jp"]}
    summary = market._competitor_summary(aio_cur, aio_prv)
    findings = market._competitor_findings(
        "branding-tokyo", "ブランディング 会社 東京", summary,
        domain_periods={}, cross_market_counts={}, cur_end="2026-07-26",
    )
    new_finding = next(f for f in findings if f["id"] == "competitor-branding-tokyo-new-new-competitor.com")
    assert new_finding["severity"] == "info"
    assert new_finding["judgment"] == "継続観測"


def test_competitor_findings_recurring_domain_is_warning():
    """直近4回中2回以上引用されているドメインはseverity=warning（Priority Action対象）に昇格する。"""
    aio_cur = {"query": "q", "acquisition_status": "success", "cited_domains": ["recurring.com"]}
    aio_prv = {"query": "q", "acquisition_status": "success", "cited_domains": []}
    summary = market._competitor_summary(aio_cur, aio_prv)
    findings = market._competitor_findings(
        "id1", "q", summary,
        domain_periods={"recurring.com": {"2026-07-19", "2026-07-26"}},
        cross_market_counts={}, cur_end="2026-07-26",
    )
    new_finding = next(f for f in findings if "new-recurring.com" in f["id"])
    assert new_finding["severity"] == "warning"
    assert new_finding["judgment"] == "再調査"


def test_competitor_findings_multi_market_domain_is_warning():
    """当週、複数の重点市場で引用されているドメインもseverity=warningに昇格する。"""
    aio_cur = {"query": "q", "acquisition_status": "success", "cited_domains": ["cross.com"]}
    aio_prv = {"query": "q", "acquisition_status": "success", "cited_domains": []}
    summary = market._competitor_summary(aio_cur, aio_prv)
    findings = market._competitor_findings(
        "id1", "q", summary,
        domain_periods={}, cross_market_counts={"cross.com": 2}, cur_end="2026-07-26",
    )
    new_finding = next(f for f in findings if "new-cross.com" in f["id"])
    assert new_finding["severity"] == "warning"


def test_competitor_findings_empty_without_previous_data():
    aio_cur = {"query": "q", "acquisition_status": "success", "cited_domains": ["x.com"]}
    summary = market._competitor_summary(aio_cur, None)
    findings = market._competitor_findings("id1", "q", summary, domain_periods={}, cross_market_counts={}, cur_end="2026-07-26")
    assert findings == []


# ── pages.py ───────────────────────────────────────────────────

PAGES_CFG = [
    {"id": "service-branding", "url": "https://tcd.jp/service/branding.html", "group": "service",
     "category": "definition", "objective": ["knowledge"], "enabled": True},
    {"id": "service-corporate_branding", "url": "https://tcd.jp/service/corporate_branding.html", "group": "service",
     "category": "service", "objective": ["conversion"], "enabled": True},
]


def test_analyze_pages_never_seen_is_warning():
    bundle = {
        "gsc": {"current": {"gsc_page_daily": [], "gsc_page_query_daily": []}, "comparison": {"gsc_page_daily": []}},
        "aio_observations": [],
    }
    findings = pages.analyze_pages(bundle, PAGES_CFG, THRESHOLDS)
    assert all(f["severity"] == "warning" for f in findings)
    assert all(f["confidence"] == "Not assessable" for f in findings)


def test_analyze_pages_conversion_page_zero_clicks_escalates():
    bundle = {
        "gsc": {
            "current": {
                "gsc_page_daily": [{"page_id": "service-corporate_branding", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 6.0}],
                "gsc_page_query_daily": [],
            },
            "comparison": {"gsc_page_daily": [{"page_id": "service-corporate_branding", "clicks": 0, "impressions": 200, "ctr": 0.0, "position": 6.0}]},
        },
        "aio_observations": [],
    }
    findings = pages.analyze_pages(bundle, [PAGES_CFG[1]], THRESHOLDS)
    assert findings[0]["severity"] == "warning"
    assert "クリックがない" in findings[0]["message"]


# ── initiatives.py ─────────────────────────────────────────────

def test_parse_initiatives_md(tmp_path):
    md = tmp_path / "current-initiatives.md"
    md.write_text(
        "# 施策管理\n\n"
        "## テスト施策\n\n"
        "- 対象URL: https://tcd.jp/branding/03-news/osaka-branding-company.html\n"
        "- 状態: 実装済み・効果確認待ち\n"
        "- 実装日: unknown\n"
        "- 評価予定日: 未設定\n"
        "- メモ: テスト\n",
        encoding="utf-8",
    )
    result = initiatives.parse_initiatives_md(md)
    assert len(result) == 1
    assert result[0]["name"] == "テスト施策"
    assert result[0]["implemented_at"] == "unknown"
    assert result[0]["target_url"] == "https://tcd.jp/branding/03-news/osaka-branding-company.html"


def test_analyze_initiatives_unknown_date_is_hold(tmp_path):
    md = tmp_path / "current-initiatives.md"
    md.write_text("## 施策A\n\n- 対象: x\n- 状態: 実装済み\n- 実装日: unknown\n- 評価予定日: 未設定\n", encoding="utf-8")
    bundle = {"period": {"cur_end": "2026-07-26"}, "gsc": {"current": {"gsc_page_daily": []}, "comparison": {"gsc_page_daily": []}}}
    findings = initiatives.analyze_initiatives(md, bundle, [], THRESHOLDS)
    assert findings[0]["judgment"] == "判断保留"
    assert findings[0]["confidence"] == "Not assessable"


def test_analyze_initiatives_before_day14_is_awaiting(tmp_path):
    md = tmp_path / "current-initiatives.md"
    md.write_text("## 施策B\n\n- 対象: x\n- 状態: 実装済み\n- 実装日: 2026-07-20\n- 評価予定日: 未設定\n", encoding="utf-8")
    bundle = {"period": {"cur_end": "2026-07-26"}, "gsc": {"current": {"gsc_page_daily": []}, "comparison": {"gsc_page_daily": []}}}
    findings = initiatives.analyze_initiatives(md, bundle, [], THRESHOLDS)
    assert findings[0]["judgment"] == "効果確認待ち"


def test_analyze_initiatives_after_day28_negative_signal_is_reinvestigate(tmp_path):
    md = tmp_path / "current-initiatives.md"
    md.write_text(
        "## 施策C\n\n- 対象URL: https://tcd.jp/branding/03-news/osaka-branding-company.html\n"
        "- 状態: 実装済み\n- 実装日: 2026-05-01\n- 評価予定日: 未設定\n",
        encoding="utf-8",
    )
    pages_cfg = [{"id": "branding-osaka-branding-company", "url": "https://tcd.jp/branding/03-news/osaka-branding-company.html"}]
    bundle = {
        "period": {"cur_end": "2026-07-26"},
        "gsc": {
            "current": {"gsc_page_daily": [{"page_id": "branding-osaka-branding-company", "clicks": 1, "impressions": 30, "ctr": 0.03, "position": 12.0}]},
            "comparison": {"gsc_page_daily": [{"page_id": "branding-osaka-branding-company", "clicks": 5, "impressions": 80, "ctr": 0.06, "position": 9.0}]},
        },
    }
    findings = initiatives.analyze_initiatives(md, bundle, pages_cfg, THRESHOLDS)
    assert findings[0]["judgment"] == "再調査"
    assert findings[0]["severity"] == "warning"


# ── landing_pages.py ─────────────────────────────────────────────

def _landing_row(url, page_id=None, clicks=0, impressions=0, position=10.0, date="2026-07-20"):
    return {
        "date": date, "page_url": url, "normalized_url": url, "page_id": page_id,
        "clicks": clicks, "impressions": impressions, "ctr": 0.0, "position": position,
    }


def _landing_bundle(cur_rows, prv_rows=None):
    return {
        "gsc": {
            "current": {"gsc_page_daily": cur_rows},
            "comparison": {"gsc_page_daily": prv_rows or []},
        },
    }


def test_landing_pages_excludes_registered_pages():
    rows = [
        _landing_row("https://tcd.jp/branding/definition/x.html", page_id="branding-x", clicks=100),
        _landing_row("https://tcd.jp/branding/03-news/new-article.html", page_id=None, clicks=10),
    ]
    bundle = _landing_bundle(rows)
    findings = landing_pages.analyze_landing_pages(bundle, None, THRESHOLDS)
    urls = [f["evidence"]["url"] for f in findings]
    assert "https://tcd.jp/branding/definition/x.html" not in urls
    assert "https://tcd.jp/branding/03-news/new-article.html" in urls


def test_landing_pages_excludes_non_branding_paths():
    rows = [_landing_row("https://tcd.jp/service/branding.html", page_id=None, clicks=50)]
    bundle = _landing_bundle(rows)
    findings = landing_pages.analyze_landing_pages(bundle, None, THRESHOLDS)
    assert findings == []


def test_landing_pages_excludes_configured_urls():
    rows = [
        _landing_row("https://tcd.jp/branding/03-news/a.html", clicks=100),
        _landing_row("https://tcd.jp/branding/03-news/b.html", clicks=50),
    ]
    bundle = _landing_bundle(rows)
    excluded_cfg = {"excluded_urls": [{"url": "https://tcd.jp/branding/03-news/a.html", "reason": "test"}]}
    findings = landing_pages.analyze_landing_pages(bundle, excluded_cfg, THRESHOLDS)
    urls = [f["evidence"]["url"] for f in findings]
    assert "https://tcd.jp/branding/03-news/a.html" not in urls
    assert "https://tcd.jp/branding/03-news/b.html" in urls


def test_landing_pages_ranked_by_clicks_top_n():
    rows = [_landing_row(f"https://tcd.jp/branding/03-news/p{i}.html", clicks=i) for i in range(1, 13)]
    bundle = _landing_bundle(rows)
    findings = landing_pages.analyze_landing_pages(bundle, None, THRESHOLDS, top_n=10)
    assert len(findings) == 10
    assert findings[0]["evidence"]["url"] == "https://tcd.jp/branding/03-news/p12.html"
    assert findings[0]["evidence"]["rank"] == 1


def test_landing_pages_empty_without_candidates():
    bundle = _landing_bundle([])
    findings = landing_pages.analyze_landing_pages(bundle, None, THRESHOLDS)
    assert findings == []
