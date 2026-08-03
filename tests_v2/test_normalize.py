import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2 import normalize


def test_normalize_url_https_unify():
    result = normalize.normalize_url("http://tcd.jp/service/branding.html")
    assert result["normalized_url"] == "https://tcd.jp/service/branding.html"


def test_normalize_url_strips_index_html():
    result = normalize.normalize_url("https://tcd.jp/branding/index.html")
    assert result["normalized_url"] == "https://tcd.jp/branding/"


def test_normalize_url_removes_utm_params():
    result = normalize.normalize_url("https://tcd.jp/service/branding.html?utm_source=x&utm_medium=y")
    assert result["normalized_url"] == "https://tcd.jp/service/branding.html"


def test_normalize_url_keeps_trailing_slash():
    result = normalize.normalize_url("https://tcd.jp/branding/")
    assert result["normalized_url"] == "https://tcd.jp/branding/"


def test_normalize_url_flags_preview_as_excluded():
    result = normalize.normalize_url("https://tcd.jp/?customize_changeset_uuid=abc123")
    assert result["is_excluded"] is True


def test_normalize_url_does_not_merge_hyphen_and_underscore():
    a = normalize.normalize_url("https://tcd.jp/service/about_branding_company.html")
    b = normalize.normalize_url("https://tcd.jp/service/about-branding-company.html")
    assert a["normalized_url"] != b["normalized_url"]


def test_normalize_query_text_strips_all_spaces():
    assert normalize.normalize_query_text("ブランディング 会社 東京") == "ブランディング会社東京"
    assert normalize.normalize_query_text("ブランディング　会社　東京") == "ブランディング会社東京"
    assert normalize.normalize_query_text("ブランディング会社東京") == "ブランディング会社東京"


def test_matches_all_tokens_exact_phrase():
    assert normalize.matches_all_tokens("ブランディング 会社 東京", ["ブランディング", "会社", "東京"]) is True


def test_matches_all_tokens_order_independent():
    # 語順が違っても、全トークンが含まれていればマッチする（2026-07-27の方針転換）
    assert normalize.matches_all_tokens("東京 インナーブランディング会社", ["インナーブランディング", "会社", "東京"]) is True
    assert normalize.matches_all_tokens("インナーブランディング会社 東京", ["インナーブランディング", "会社", "東京"]) is True
    assert normalize.matches_all_tokens("東京のインナーブランディング会社", ["インナーブランディング", "会社", "東京"]) is True


def test_matches_all_tokens_partial_is_not_a_match():
    # 「インナーブランディング 支援」のようにtokenの一部しか含まないクエリは対象外
    assert normalize.matches_all_tokens("インナーブランディング 支援", ["インナーブランディング", "会社", "東京"]) is False


def test_matches_all_tokens_unrelated_query_is_not_a_match():
    assert normalize.matches_all_tokens("インナーブランディング 会社 大阪", ["ブランディング", "会社", "東京"]) is False


def test_build_page_lookup_resolves_page_id():
    pages = [{"id": "service-branding", "url": "https://tcd.jp/service/branding.html"}]
    lookup = normalize.build_page_lookup(pages)
    assert lookup["https://tcd.jp/service/branding.html"] == "service-branding"


def test_build_query_daily_from_raw():
    raw = {
        "dimensions": ["date", "query"],
        "fetched_at": "2026-07-27T00:00:00Z",
        "response": {"rows": [
            {"keys": ["2026-07-20", "ブランディング 会社 東京"], "clicks": 3, "impressions": 100, "ctr": 0.03, "position": 5.2},
        ]},
    }
    out = normalize.build_query_daily(raw, raw["fetched_at"])
    assert out[0]["query"] == "ブランディング 会社 東京"
    assert out[0]["normalized_query"] == "ブランディング会社東京"
    assert out[0]["clicks"] == 3


def test_build_page_daily_resolves_unregistered_page_as_none():
    raw = {
        "dimensions": ["date", "page"],
        "fetched_at": "2026-07-27T00:00:00Z",
        "response": {"rows": [
            {"keys": ["2026-07-20", "https://tcd.jp/unregistered/page.html"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 8.0},
        ]},
    }
    out = normalize.build_page_daily(raw, {}, raw["fetched_at"])
    assert out[0]["page_id"] is None


def test_build_aio_observation_success_with_tcd_citation():
    record = {
        "observed_at": "2026-07-27T00:00:00Z",
        "query": "ブランディング 会社 東京",
        "acquisition_status": "success",
        "raw_response": {
            "search_result": {
                "ai_overview": {
                    "text": "TCDはブランディング会社です。",
                    "references": [
                        {"link": "https://tcd.jp/service/corporate_branding.html", "source": {"name": "TCD"}, "title": "TCD"},
                        {"link": "https://paddledesign.co.jp/", "source": {"name": "パドルデザインカンパニー"}, "title": "Paddle"},
                    ],
                }
            },
            "ai_overview_result": None,
        },
        "error_message": None,
    }
    competitor_domains_cfg = {
        "aliases": {"paddledesign.co.jp": ["paddledesign.co.jp", "パドルデザインカンパニー"]},
    }
    obs = normalize.build_aio_observation(record, competitor_domains_cfg)
    assert obs["aio_generated"] is True
    assert obs["tcd_cited"] is True
    assert "https://tcd.jp/service/corporate_branding.html" in obs["tcd_urls"]
    assert "paddledesign.co.jp" in obs["cited_domains"]
    assert obs["answer_text_hash"] is not None


def test_build_aio_observation_not_generated():
    record = {
        "observed_at": "2026-07-27T00:00:00Z",
        "query": "ブランディング 会社 東京",
        "acquisition_status": "not_generated",
        "raw_response": {"search_result": {}, "ai_overview_result": None},
        "error_message": None,
    }
    obs = normalize.build_aio_observation(record)
    assert obs["aio_generated"] is False
    assert obs["tcd_cited"] is None


def test_build_aio_observation_failed_has_null_fields():
    record = {
        "observed_at": "2026-07-27T00:00:00Z",
        "query": "ブランディング 会社 東京",
        "acquisition_status": "failed",
        "raw_response": None,
        "error_message": "timeout",
    }
    obs = normalize.build_aio_observation(record)
    assert obs["aio_generated"] is None
    assert obs["tcd_cited"] is None
    assert obs["error_message"] == "timeout"
