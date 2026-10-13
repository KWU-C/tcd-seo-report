import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2.report.render_html import render_html
from src_v2.report.render_json import render_json
from src_v2.report.render_md import render_markdown
from src_v2.report.view_model import build_view_model

_THRESHOLDS = {
    "low_data": {"low_impressions": 50, "low_clicks": 5, "position_change_meaningful": 2.0, "minimum_weeks_for_trend": 3},
    "display": {"position_delta_flat": 1.0, "position_delta_minor": 3.0, "access_change_pct": 5.0},
}


def _finding(id, category, severity, confidence, message="msg", recommendation="rec", judgment=None,
             evidence=None, source=None):
    f = {"id": id, "category": category, "severity": severity, "confidence": confidence,
         "message": message, "recommendation": recommendation}
    if judgment:
        f["judgment"] = judgment
    if evidence is not None:
        f["evidence"] = evidence
    if source is not None:
        f["source"] = source
    return f


def _page_evidence(group="service", category="service", cur_impr=100, cur_clicks=5, cur_pos=8.0,
                    prv_impr=100, prv_clicks=5, prv_pos=8.0, url=None, objective=None):
    return {
        "group": group, "category": category, "title": f"page-{id(object())}", "url": url,
        "objective": objective or [],
        "current": {"impressions": cur_impr, "clicks": cur_clicks, "position": cur_pos},
        "comparison": {"impressions": prv_impr, "clicks": prv_clicks, "position": prv_pos},
    }


def _market_evidence(query="市場クエリ", cur_impr=100, cur_clicks=5, cur_pos=8.0, prv_pos=8.0, top_url=None):
    return {
        "query": query, "market": "branding", "region": "tokyo", "match_tokens": ["市場", "クエリ"],
        "current": {"impressions": cur_impr, "clicks": cur_clicks, "position": cur_pos},
        "comparison": {"impressions": cur_impr, "clicks": cur_clicks, "position": prv_pos},
        "matched_query_variants": [], "top_url": {"page_id_or_url": top_url} if top_url else None,
        "aio": {"aio_generated": True, "tcd_cited": False, "cited_domains": [], "acquisition_status": "success"},
        "competitor_summary": {"comparison_available": False},
    }


def _analysis(findings):
    return {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-27T00:00:00Z",
        "period": {"cur_start": "2026-07-20", "cur_end": "2026-07-26", "prv_start": "2026-07-13", "prv_end": "2026-07-19"},
        "data_quality": {"status": "pass", "errors": [], "warnings": [], "aio_success_rate": 1.0},
        "data_summary": {
            "gsc": {"query_daily_rows": 100, "page_daily_rows": 50, "page_query_daily_rows": 80, "comparison_available": True},
            "aio": {"success": 4, "not_generated": 0, "not_observed": 0, "failed": 0, "not_run": 0},
            "magazine_landing_pages": {"excluded_urls": []},
        },
        "weeks_available": 1,
        "ga4_summary": {},
        "findings": findings,
        "counts": {
            "total": len(findings),
            "by_severity": {s: sum(1 for f in findings if f["severity"] == s) for s in {"critical", "warning", "info"}},
            "by_category": {},
        },
    }


def _vm(findings):
    return build_view_model(_analysis(findings), thresholds_cfg=_THRESHOLDS, notes_cfg=[])


# ── 重点キーワード（③SEO検索順位・④AIO引用状況・Appendixキーワード別詳細の元データ） ──

def _cards(vm):
    return [c for g in vm["priority_markets"]["groups"] for c in g["cards"]]


def test_priority_markets_includes_market_and_nested_competitor():
    findings = [
        _finding("market-1", "market", "warning", "Low", evidence=_market_evidence(), source={"query_id": "q1", "query": "市場クエリ"}),
        _finding("competitor-1", "competitor", "warning", "Medium", source={"query_id": "q1", "query": "市場クエリ"}),
    ]
    vm = _vm(findings)
    cards = _cards(vm)
    assert len(cards) == 1
    assert cards[0]["id"] == "market-1"
    assert [c["id"] for c in cards[0]["competitor_findings"]] == ["competitor-1"]


def test_priority_markets_excludes_page_category():
    findings = [_finding("page-1", "page", "critical", "High", evidence=_page_evidence())]
    vm = _vm(findings)
    assert _cards(vm) == []
    assert vm["priority_markets"]["table"] == []


# ── Appendix「少数データ・要確認以外のページ」の振り分け ─────────────────
# 本文のカテゴリ別動向・順位変動上位は2026-09-28に削除済み。注目ページの判定は
# Appendixのページ一覧（other_pages）から除外するかどうかにのみ使われている。

def test_notable_page_requires_impressions_and_position_delta():
    # Impressions>=50 かつ 順位差>=3 → notable
    big = _finding("page-notable", "page", "warning", "Medium", judgment="再調査",
                    evidence=_page_evidence(cur_impr=100, prv_pos=20.0, cur_pos=10.0))
    # Impressionsが少ない → notableではない（Appendix行き）
    small = _finding("page-small", "page", "info", "Low",
                      evidence=_page_evidence(cur_impr=5, prv_pos=20.0, cur_pos=10.0))
    vm = _vm([big, small])
    other_ids = {f["id"] for f in vm["appendix"]["other_pages"]}
    assert "page-notable" not in other_ids
    assert "page-small" in other_ids


def test_notable_page_click_delta_qualifies_even_with_low_impressions():
    f = _finding("page-clicks", "page", "warning", "Medium", judgment="再調査",
                 evidence=_page_evidence(cur_impr=5, cur_clicks=5, prv_clicks=1, prv_pos=10.0, cur_pos=10.0))
    vm = _vm([f])
    assert "page-clicks" not in {x["id"] for x in vm["appendix"]["other_pages"]}


def test_notable_page_priority_market_url_always_included():
    market = _finding("market-1", "market", "info", "Low",
                       evidence=_market_evidence(top_url="https://tcd.jp/target.html"),
                       source={"query_id": "q1", "query": "市場クエリ"})
    page = _finding("page-target", "page", "info", "Low",
                     evidence=_page_evidence(url="https://tcd.jp/target.html", cur_impr=1, prv_pos=10.0, cur_pos=10.2))
    vm = _vm([market, page])
    assert "page-target" not in {x["id"] for x in vm["appendix"]["other_pages"]}


def test_notable_page_conversion_objective_always_included():
    page = _finding("page-conv", "page", "info", "Low",
                     evidence=_page_evidence(cur_impr=1, objective=["conversion"], prv_pos=10.0, cur_pos=10.2))
    vm = _vm([page])
    assert "page-conv" not in {x["id"] for x in vm["appendix"]["other_pages"]}


# ── Appendix「実装日未登録の施策」 ─────────────────────────────────

def test_only_initiatives_without_implemented_date_are_listed_in_appendix():
    known = _finding("initiative-known", "initiative", "info", "Medium", judgment="効果確認待ち",
                      evidence={"implemented_at": "2026-07-01", "current": {}, "comparison": {}})
    unknown = _finding("initiative-unknown", "initiative", "info", "Not assessable", judgment="判断保留",
                        evidence={"name": "x", "status": "実装済み", "target": "y"})
    vm = _vm([known, unknown])
    assert [f["id"] for f in vm["appendix"]["unknown_initiatives"]] == ["initiative-unknown"]


# ── header / appendix ────────────────────────────────────────────

def test_header_fields_present():
    vm = _vm([_finding("page-1", "page", "warning", "Medium", evidence=_page_evidence())])
    header = vm["header"]
    for key in ("period", "comparison_available", "data_quality_status", "data_quality_label", "weeks_available"):
        assert key in header


def test_appendix_data_quality_fields_present():
    vm = _vm([])
    dq = vm["appendix"]["data_quality"]
    for key in ("gsc_counts", "aio_status", "finding_count", "severity_breakdown", "confidence_breakdown"):
        assert key in dq


def test_render_json_md_html_reference_same_data():
    findings = [
        _finding("market-1", "market", "warning", "Low", message="AIO未引用", judgment="判断保留",
                 evidence=_market_evidence(), source={"query_id": "q1", "query": "市場クエリ"}),
        _finding("page-1", "page", "warning", "Medium", message="ページ変化", judgment="再調査",
                 evidence=_page_evidence(cur_impr=100, prv_pos=15.0, cur_pos=5.0)),
        _finding("initiative-1", "initiative", "info", "Not assessable", message="施策メッセージ", judgment="判断保留",
                 evidence={"name": "x"}),
    ]
    vm = _vm(findings)

    json_out = render_json(vm)
    md_out = render_markdown(vm)
    html_out = render_html(vm)

    assert "AIO未引用" in json_out
    assert "AIO未引用" in md_out
    assert "AIO未引用" in html_out
    assert "施策メッセージ" in md_out
    assert "施策メッセージ" in html_out

    for label in ("① サイト全体のアクセス状況", "② 今週の要点", "③ SEO検索順位", "④ AIO引用状況",
                  "キーワード別詳細", "実装日未登録の施策", "上位ランディングページ"):
        assert label in md_out
        assert label in html_out


def test_narrative_overrides_blocks_when_present():
    vm = _vm([])
    vm["executive_summary"]["narrative"] = "今週は大きな変化はなく、データ蓄積を継続する段階です。"
    md_out = render_markdown(vm)
    html_out = render_html(vm)
    assert "今週は大きな変化はなく、データ蓄積を継続する段階です。" in md_out
    assert "今週は大きな変化はなく、データ蓄積を継続する段階です。" in html_out
    # 数値から組み立てた要点はnarrative指定時は表示されない
    assert vm["executive_summary"]["blocks"] == [] or vm["executive_summary"]["blocks"][0] not in md_out


def test_keyword_recommendation_rendered_in_md_and_html():
    findings = [_finding("market-1", "market", "warning", "Medium", message="市場の変化", recommendation="市場対応",
                          judgment="再調査", evidence=_market_evidence())]
    vm = _vm(findings)
    md_out = render_markdown(vm)
    html_out = render_html(vm)
    assert "市場対応" in md_out
    assert "市場対応" in html_out


def test_render_html_escapes_message():
    findings = [_finding("market-1", "market", "critical", "High", message="<script>alert(1)</script>",
                          evidence=_market_evidence())]
    vm = _vm(findings)
    html_out = render_html(vm)
    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;" in html_out


# ── Appendix: 上位ランディングページ ────────────────────────────

def _landing_finding(rank, url, clicks=10):
    return _finding(
        f"landing-{rank}", "magazine_landing_page", "info", "Low",
        message=f"第{rank}位: クリック{clicks}件",
        recommendation="継続観測する",
        judgment="継続観測",
        evidence={"url": url, "rank": rank, "current": {"clicks": clicks, "impressions": clicks * 10}},
    )


def test_landing_pages_preserve_rank_order_not_resorted():
    findings = [_landing_finding(1, "https://tcd.jp/branding/a.html"), _landing_finding(2, "https://tcd.jp/branding/b.html")]
    vm = _vm(findings)
    ranks = [f["evidence"]["rank"] for f in vm["appendix"]["landing_pages"]["top"]]
    assert ranks == [1, 2]


def test_landing_pages_excluded_list_passed_through():
    analysis = _analysis([])
    analysis["data_summary"]["magazine_landing_pages"]["excluded_urls"] = [
        {"url": "https://tcd.jp/branding/excluded.html", "reason": "採用ページ"}
    ]
    vm = build_view_model(analysis, thresholds_cfg=_THRESHOLDS)
    assert vm["appendix"]["landing_pages"]["excluded"][0]["url"] == "https://tcd.jp/branding/excluded.html"


def test_landing_pages_not_mixed_into_other_pages():
    findings = [_landing_finding(1, "https://tcd.jp/branding/a.html")]
    vm = _vm(findings)
    assert vm["appendix"]["other_pages"] == []


def test_render_md_and_html_include_landing_pages_in_appendix():
    findings = [_landing_finding(1, "https://tcd.jp/branding/a.html", clicks=42)]
    analysis = _analysis(findings)
    analysis["data_summary"]["magazine_landing_pages"]["excluded_urls"] = [
        {"url": "https://tcd.jp/branding/excluded.html", "reason": "採用ページのため対象外"}
    ]
    vm = build_view_model(analysis, thresholds_cfg=_THRESHOLDS)
    md_out = render_markdown(vm)
    html_out = render_html(vm)

    assert "上位ランディングページ" in md_out and "上位ランディングページ" in html_out
    assert "excluded.html" in md_out and "excluded.html" in html_out
    assert "採用ページのため対象外" in md_out and "採用ページのため対象外" in html_out


# ── 2026-10-13改善指示書: アクセス → SEO順位 → AIO引用 → Appendix ──────

def _keyword_finding(id, label_region, cur_pos, prv_pos, cur_impr, min_impr=50, citations=None, tcd_cited=False, tier="deal"):
    evidence = _market_evidence(cur_impr=cur_impr, cur_pos=cur_pos, prv_pos=prv_pos)
    evidence.update({"region": label_region, "tier": tier, "min_impressions": min_impr, "search_volume_ref": 1000,
                     "search_volume_as_of": "2026-07"})
    evidence["aio"]["tcd_cited"] = tcd_cited
    if citations is not None:
        evidence["aio_citations"] = citations
    return _finding(id, "market", "info", "Medium", judgment="継続観測", evidence=evidence)


def _row(vm, region_label):
    return next(r for r in vm["priority_markets"]["table"] if r["short_label"] == region_label)


def test_sections_are_ordered_access_seo_aio_appendix():
    vm = _vm([_keyword_finding("market-tokyo", "tokyo", 9.0, 10.3, 126,
                               citations={"status": "extracted", "domains": ["a.com"]})])
    for out in (render_markdown(vm), render_html(vm)):
        positions = [out.index(h) for h in ("① サイト全体のアクセス状況", "② 今週の要点", "③ SEO検索順位", "④ AIO引用状況", "Appendix　※クリックして表示")]
        assert positions == sorted(positions)
        assert "重点商談市場" not in out and "テーマ市場" not in out


def test_seo_delta_label_improved_and_low_impressions_is_reference_only():
    vm = _vm([
        _keyword_finding("market-tokyo", "tokyo", 9.0, 10.3, 126),
        _keyword_finding("market-osaka", "osaka", 10.8, 9.2, 31, min_impr=35),
    ])
    assert _row(vm, "東京")["delta_label"] == "+1.3 改善"
    # 最低Imp未満は1週間の順位差を「悪化」と呼ばない
    assert _row(vm, "大阪")["delta_label"] == "−1.6（参考値）"
    assert _row(vm, "大阪")["seo_movement"] == "参考値"


def test_seo_row_without_impressions_is_shown_as_no_gsc_data():
    vm = _vm([_keyword_finding("market-tokyo", "tokyo", None, None, 0)])
    assert _row(vm, "東京")["delta_label"] == "—"
    assert "GSCで表示データなし" in render_markdown(vm)
    assert "GSCで表示データなし" in render_html(vm)


def test_aio_citation_unavailable_is_not_reported_as_not_cited():
    vm = _vm([_keyword_finding("market-tokyo", "tokyo", 9.0, 9.0, 126, citations={"status": "unavailable", "domains": []})])
    row = _row(vm, "東京")
    assert (row["aio_generated_label"], row["tcd_cited_label"]) == ("未確認", "判定不可")
    assert "取得不可" in row["aio_sources_label"]
    assert "東京はAIO本文を取得できず判定不可" in vm["executive_summary"]["blocks"][-1]


def test_aio_sources_show_top_three_in_body_and_all_in_appendix():
    domains = ["a.com", "b.com", "c.com", "d.com"]
    vm = _vm([_keyword_finding("market-tokyo", "tokyo", 9.0, 9.0, 126, citations={"status": "extracted", "domains": domains})])
    assert _row(vm, "東京")["aio_sources_label"] == "a.com、b.com、c.com など"
    assert "d.com" in render_markdown(vm) and "d.com" in render_html(vm)


def test_highlights_are_capped_at_three_and_start_with_access():
    analysis = _analysis([_keyword_finding("market-tokyo", "tokyo", 9.0, 10.3, 126, tcd_cited=True,
                                           citations={"status": "extracted", "domains": ["a.com"]})])
    analysis["ga4_summary"] = {
        "site_wide": {"label": "サイト全体", "current": 1387, "comparison": 1421},
        "works": {"label": "Works到達", "current": 266, "comparison": 231},
        "magazine": {"label": "マガジン到達", "current": 486, "comparison": 469},
    }
    blocks = build_view_model(analysis, thresholds_cfg=_THRESHOLDS)["executive_summary"]["blocks"]
    assert len(blocks) == 3
    assert blocks[0] == "サイト全体は1,387（前週比−2.4%）。Worksへの到達は増加。"
    assert "東京9.0位に改善（前週10.3位）" in blocks[1]
    assert blocks[2] == "AIOは東京でTCD引用あり。"


def test_data_quality_label_names_keywords_whose_aio_content_was_not_retrieved():
    vm = _vm([_keyword_finding("market-tokyo", "tokyo", 9.0, 9.0, 126, citations={"status": "unavailable", "domains": []})])
    assert vm["header"]["data_quality_label"] == "pass（AIO 1件中1件は本文取得不可：東京）"
    for out in (render_markdown(vm), render_html(vm)):
        assert "pass（AIO 1件中1件は本文取得不可：東京）" in out


def test_data_quality_label_is_plain_status_when_all_aio_content_retrieved():
    vm = _vm([_keyword_finding("market-tokyo", "tokyo", 9.0, 9.0, 126, citations={"status": "extracted", "domains": ["a.com"]})])
    assert vm["header"]["data_quality_label"] == "pass"


def test_period_note_is_shown_only_for_reports_in_its_window():
    notes = [{"section": "aio", "after_cur_end": "2026-07-20", "until_cur_end": "2026-07-26", "text": "前回判定の訂正"}]
    finding = _keyword_finding("market-tokyo", "tokyo", 9.0, 9.0, 126)
    shown = build_view_model(_analysis([finding]), thresholds_cfg=_THRESHOLDS, notes_cfg=notes)
    assert shown["priority_markets"]["aio_notes"][0] == "前回判定の訂正"
    assert "前回判定の訂正" in render_markdown(shown) and "前回判定の訂正" in render_html(shown)

    expired = [{**notes[0], "until_cur_end": "2026-07-25"}]
    hidden = build_view_model(_analysis([finding]), thresholds_cfg=_THRESHOLDS, notes_cfg=expired)
    assert "前回判定の訂正" not in render_markdown(hidden)
