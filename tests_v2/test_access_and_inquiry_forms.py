"""① アクセス（指標名・流入元・異常値）と ② 問い合わせ（案件相談率・フォーム区分）の改修（2026-11-23週〜）。"""
from src_v2 import config_loader
from src_v2.analysis.access import build_access_summary, detect_anomaly
from src_v2.fetch_ga4 import segment_filter
from src_v2.fetch_inquiries import ROW_CONFIRMED, parse_row
from src_v2.report import view_model
from src_v2.report.render_html import render_html
from src_v2.report.render_md import render_markdown

ANOMALY = {"min_week_users": 30, "min_day_share": 0.5, "min_ratio_to_median": 3.0}
THRESHOLDS = {"access_anomaly": ANOMALY, "display": {"access_change_pct": 5.0}}
PERIOD = {"report_week_start": "2026-11-09", "report_week_end": "2026-11-15",
          "ga4_prv_start": "2026-11-02", "ga4_prv_end": "2026-11-08", "cur_start": "2026-11-07", "cur_end": "2026-11-13"}


def _daily(values: dict[str, int]) -> dict:
    return {f"2026-11-{d:02d}": v for d, v in values.items()}


# ── ページ条件（部分一致による混入の防止） ─────────────────────────

def _paths(expr):
    if expr.filter.field_name:
        return [(expr.filter.field_name, list(expr.filter.in_list_filter.values) or expr.filter.string_filter.value)]
    return [p for e in expr.or_group.expressions for p in _paths(e)]


def test_download_segments_are_exact_and_separate():
    cfg = {s["key"]: s for s in config_loader.load_pipeline_config()["ga4"]["segments"]}
    assert _paths(segment_filter(cfg["document_request"])) == [("pagePath", ["/download", "/download/"])]
    assert _paths(segment_filter(cfg["b2b_document_request"])) == [("pagePath", ["/b2b/download", "/b2b/download/"])]
    # /service トップを含み、/works/service/* を含まない（前方一致）
    service = _paths(segment_filter(cfg["service"]))
    assert ("pagePath", ["/service"]) in service and ("pagePath", "/service/") in service
    assert all(not s.get("path_contains") for s in cfg.values())


def test_legacy_path_contains_still_readable():
    expr = segment_filter({"path_contains": "/download"})
    assert expr.filter.field_name == "pagePathPlusQueryString"


# ── 異常値 ──────────────────────────────────────────────────────

def test_single_day_spike_is_flagged():
    daily = _daily({9: 6, 10: 3, 11: 12, 12: 135, 13: 6, 14: 0, 15: 2})
    a = detect_anomaly(daily, "2026-11-09", "2026-11-15", 164, ANOMALY)
    assert a == {"date": "2026-11-12", "day_users": 135, "days_total": 164, "week_users": 164}


def test_sustained_increase_is_not_flagged():
    daily = _daily({9: 176, 10: 267, 11: 670, 12: 796, 13: 595, 14: 637, 15: 704})
    assert detect_anomaly(daily, "2026-11-09", "2026-11-15", 3741, ANOMALY) is None


def test_small_segment_is_not_flagged():
    assert detect_anomaly(_daily({12: 20}), "2026-11-09", "2026-11-15", 20, ANOMALY) is None


def _bundle():
    daily_dl = _daily({2: 5, 3: 3, 4: 4, 5: 3, 6: 5, 7: 3, 8: 3, 9: 6, 10: 3, 11: 12, 12: 135, 13: 6, 14: 0, 15: 2})
    return {
        "period": PERIOD,
        "ga4": {
            "site_wide": {"label": "サイト全体", "rule_label": "すべてのページ", "group": "main", "metric": "activeUsers",
                          "current": 3741, "comparison": 1294, "daily": {}, "error": None},
            "contact": {"label": "問い合わせフォーム到達（一般）", "rule_label": "/contact（入力画面のみ）", "group": "cv",
                        "metric": "activeUsers", "current": 50, "comparison": 52, "daily": {}, "error": None},
            "b2b_contact": {"label": "問い合わせフォーム到達（BtoB）", "rule_label": "/b2b/contact（入力画面のみ）", "group": "cv",
                            "metric": "activeUsers", "current": 8, "comparison": 1, "daily": {}, "error": None},
            "document_request": {"label": "資料請求ページ到達（一般）", "rule_label": "/download（入力画面のみ）", "group": "cv",
                                 "metric": "activeUsers", "current": 154, "comparison": 26, "daily": daily_dl, "error": None},
        },
        "ga4_access": {
            "version": 2,
            "channels": {"status": "ok", "metric": "sessions", "rows": [
                {"source_medium": "universe / cpc", "channel": "ad", "ai_service": None,
                 "current": {"sessions": 2170, "engaged_sessions": 686}, "comparison": {"sessions": 0, "engaged_sessions": 0}},
                {"source_medium": "google / organic", "channel": "organic", "ai_service": None,
                 "current": {"sessions": 374, "engaged_sessions": 209}, "comparison": {"sessions": 380, "engaged_sessions": 200}},
                {"source_medium": "(direct) / (none)", "channel": "direct", "ai_service": None,
                 "current": {"sessions": 897, "engaged_sessions": 271}, "comparison": {"sessions": 500, "engaged_sessions": 150}},
                {"source_medium": "chatgpt.com / ai-assistant", "channel": "ai", "ai_service": "ChatGPT",
                 "current": {"sessions": 21, "engaged_sessions": 10}, "comparison": {"sessions": 30, "engaged_sessions": 12}},
            ]},
            "trend": {"status": "ok", "weeks": [
                {"week_start": "2026-11-02", "users_total": 1294, "users_non_ad": 1290, "sessions_total": 1500,
                 "sessions_ad": 0, "sessions_non_ad": 1500, "sessions_by_channel": {}},
                {"week_start": "2026-11-09", "users_total": 3741, "users_non_ad": 1700, "sessions_total": 3462,
                 "sessions_ad": 2170, "sessions_non_ad": 1292, "sessions_by_channel": {}},
            ]},
        },
    }


def test_access_summary_flags_anomaly_and_sums_channels():
    s = build_access_summary(_bundle(), THRESHOLDS)
    dl = next(x for x in s["segments"] if x["key"] == "document_request")
    assert dl["anomalies"][0]["date"] == "2026-11-12" and dl["anomalies"][0]["period"] == "current"
    ch = s["channels"]
    assert ch["total"]["current"] == 2170 + 374 + 897 + 21
    assert [r["key"] for r in ch["rows"]] == ["ad", "organic", "direct", "referral", "ai", "other"]
    assert ch["ai_services"] == [{"service": "ChatGPT", "current": 21, "comparison": 30}]


def _vm_site_usage():
    access = build_access_summary(_bundle(), THRESHOLDS)
    return view_model._build_access(access, [], "2026-11-13")


def test_site_usage_states_metric_names():
    su = _vm_site_usage()
    assert "アクティブユーザー数" in su["metric_note"] and "activeUsers" in su["metric_note"]
    assert "セッション数" in su["channel_note"] and "単位が違う" in su["channel_note"]


def test_anomaly_note_does_not_name_a_source():
    su = _vm_site_usage()
    note = su["anomaly_notes"][0]
    assert "11/12(木)の1日に135人" in note and "原因は特定していない" in note
    for word in ("直接", "広告", "メール", "流入元"):
        assert word not in note


def test_highlight_mentions_non_ad_and_skips_anomalous_segment():
    su = _vm_site_usage()
    line = view_model._access_highlight_v2(su, THRESHOLDS)
    assert line.startswith("サイト全体のアクティブユーザー数は3,741（前週比+189.1%）、広告を除くと1,700")
    assert "セッション数の前週差+2,552のうち、最も大きいのは広告の+2,170" in line
    assert "資料請求ページ到達（一般）は今週の11/12(木)の1日に集中した異常値（原因未確認）を含むため、増減を評価しない" in line
    assert "資料請求ページ（一般）への到達は増加" not in line


# ── ② 問い合わせ ─────────────────────────────────────────────────

BASE_ROW = {"week_start": "2026-11-09", "week_end": "2026-11-15", "status": "確定", "general_total": 15,
            "project_consultation": 1, "sales_solicitation": 13, "other": 1, "other_needs_review": 0, "recruiting_form": 0}
FORMS = {"form_general": 13, "form_b2b": 1, "form_unknown": 1,
         "consultation_form_general": 0, "consultation_form_b2b": 1, "consultation_form_unknown": 0}


def test_form_columns_blank_means_not_recorded():
    r = parse_row(BASE_ROW)
    assert r["state"] == ROW_CONFIRMED and r["forms"]["state"] == "not_recorded"


def test_form_columns_ok():
    r = parse_row({**BASE_ROW, **FORMS})
    assert r["forms"]["state"] == "ok"
    assert r["forms"]["project_consultation"] == {"general": 0, "b2b": 1, "unknown": 0}


def test_form_mismatch_keeps_existing_counts_valid():
    """フォーム区分の合計が合わなくても、既存の件数（Dot の集計）は確定のまま使う。"""
    r = parse_row({**BASE_ROW, **FORMS, "form_unknown": 5})
    assert r["state"] == ROW_CONFIRMED and r["general_total"] == 15
    assert r["forms"]["state"] == "invalid" and "general_total 15 と一致しない" in r["forms"]["errors"][0]


def test_partial_form_columns_are_invalid():
    r = parse_row({**BASE_ROW, "form_general": 15})
    assert r["forms"]["state"] == "invalid"


def _inquiries(row, site_usage=None):
    snapshot = {"status": "ok", "error": None, "rows": [parse_row(row)]}
    return view_model._build_weekly_inquiries([], snapshot, PERIOD, site_usage)


def test_consultation_rate_and_reach_note():
    inq = _inquiries({**BASE_ROW, **FORMS}, _vm_site_usage())
    assert inq["rate"]["current_label"] == "6.7%（1/15）"
    assert inq["rate"]["cumulative_4w_label"] == "未確定（4週中1週集計済み）"
    assert inq["forms"]["rows"][1] == {"label": "うち案件相談", "general": 0, "b2b": 1, "unknown": 0}
    reach = next(n for n in inq["notes"] if n.startswith("参考（同じ週）"))
    assert "一般50人・BtoB8人" in reach and "一般問い合わせ15件" in reach and "単位が違う" in reach


def test_invalid_forms_raise_warning_but_counts_stay():
    inq = _inquiries({**BASE_ROW, **FORMS, "form_unknown": 5})
    assert inq["status_label"] == "確定（Dot集計）"
    assert inq["forms"]["state"] == "invalid" and any("フォーム区分" in w for w in inq["warnings"])


def test_zero_general_total_rate_is_not_divided():
    row = {**BASE_ROW, "general_total": 0, "project_consultation": 0, "sales_solicitation": 0, "other": 0}
    assert _inquiries(row)["rate"]["current_label"] == "—（一般問い合わせ0件）"


def test_manual_yaml_form_mismatch_is_error(tmp_path):
    import pytest
    p = tmp_path / "inquiries.yaml"
    p.write_text("weeks:\n  - start: '2026-11-09'\n    end: '2026-11-15'\n    general_total: 15\n    project_consultation: 1\n"
                 "    sales_solicitation: 13\n    other: 1\n    other_needs_review: 0\n    recruiting_form: 0\n"
                 "    form_general: 1\n    form_b2b: 1\n    form_unknown: 1\n"
                 "    consultation_form_general: 0\n    consultation_form_b2b: 1\n    consultation_form_unknown: 0\n",
                 encoding="utf-8")
    with pytest.raises(config_loader.InquiriesConfigError):
        config_loader.load_inquiries(p)


# ── 描画（Markdown・HTML で同じ内容） ───────────────────────────────

def test_markdown_and_html_render_v2_sections():
    su = _vm_site_usage()
    inq = _inquiries({**BASE_ROW, **FORMS}, su)
    vm = {
        "header": {"periods": {"title": "2026-11-09 〜 2026-11-15", "header_note": "対象期間", "sections": {
            "site_usage": "", "inquiries": "", "seo": "", "aio": ""}, "notes": {"site_usage": [], "seo": []}},
            "data_quality_label": "pass", "weeks_available": 1, "low_impressions_threshold": 50},
        "site_usage": su, "inquiries": inq, "executive_summary": {"blocks": ["x"], "narrative": None},
        "priority_markets": {"table": [], "groups": [], "seo_notes": [], "aio_notes": []},
        "appendix": {"data_quality": {}, "other_pages": [], "unknown_initiatives": [],
                     "initiatives": {"scheme": "v2", "items": [], "notes": ["n"]},
                     "landing_pages": {"top": [], "excluded": []}, "all_findings": []},
    }
    md, html = render_markdown(vm), render_html(vm)
    for text in ("到達ユーザー（指標: GA4 アクティブユーザー数）", "流入元別（指標: GA4 セッション数）",
                 "ユーザー 広告を除く（人）", "案件相談率（案件相談÷一般問い合わせ）", "6.7%（1/15）",
                 "施策の効果確認（実装前後の比較）", "資料請求ページ到達（BtoB）" if False else "資料請求ページ到達（一般）"):
        assert text in md, text
        assert text in html, text
    assert "※異常値を含む" in md and "※異常値を含む" in html
    assert "| うち案件相談 | 0 | 1 | 0 |" in md


def test_small_segments_are_not_summarised_as_increase():
    b = _bundle()
    b["ga4"]["b2b_contact"].update(current=8, comparison=1)
    su = view_model._build_access(build_access_summary(b, THRESHOLDS), [], "2026-11-13")
    line = view_model._access_highlight_v2(su, {**THRESHOLDS, "display": {"access_change_pct": 5.0, "access_min_users": 30}})
    assert "BtoB" not in line


def test_review_notification_includes_anomaly(tmp_path):
    import json
    from src_v2.notify import collect_review_items
    su = _vm_site_usage()
    (tmp_path / "report.json").write_text(json.dumps({"header": {"data_quality_status": "pass"}, "site_usage": su,
                                                      "inquiries": {}, "appendix": {}}), encoding="utf-8")
    items = collect_review_items(tmp_path)
    assert any(i.startswith("GA4 異常値（原因未確認）: 資料請求ページ到達（一般）") for i in items)


# ── 広告を除くユーザー数・セグメント別推移・新旧の差 ─────────────────

from types import SimpleNamespace  # noqa: E402

from src_v2 import fetch_ga4  # noqa: E402


class _RecClient:
    def __init__(self, rows=None):
        self.requests = []
        self.rows = rows or []

    def run_report(self, request):
        self.requests.append(request)
        return SimpleNamespace(rows=self.rows, dimension_headers=[SimpleNamespace(name="isoYearIsoWeek")], row_count=len(self.rows))


def _row(week, value):
    return SimpleNamespace(dimension_values=[SimpleNamespace(value=week)], metric_values=[SimpleNamespace(value=str(value))])


GA4_CFG = config_loader.load_pipeline_config()["ga4"]


def test_non_ad_users_come_from_a_ga4_filtered_query_not_subtraction():
    """「広告を除く」アクティブユーザー数は、広告以外のセッションに絞った GA4 の集計（ユーザーの重複を GA4 が除く）。"""
    c = _RecClient([_row("202646", 1322)])
    fetch_ga4.fetch_trend(GA4_CFG, "2026-11-09", c)
    users_req = [r for r in c.requests if [m.name for m in r.metrics] == ["activeUsers"]]
    assert len(users_req) == 2  # 全体と、広告を除く
    non_ad = users_req[1].dimension_filter.and_group.expressions[1]
    assert non_ad.not_expression.filter.field_name == "sessionMedium"
    assert non_ad.not_expression.filter.string_filter.value == GA4_CFG["channels"]["ad_medium_regex"]


def test_segment_trend_uses_new_rules_for_all_weeks():
    c = _RecClient([_row("202640", 300), _row("202646", 433)])
    st = fetch_ga4.fetch_segment_trend(GA4_CFG, "2026-11-09", c)
    assert st["weeks"][0] == "2026-09-21" and st["weeks"][-1] == "2026-11-09"
    service_req = c.requests[[s["key"] for s in GA4_CFG["segments"]].index("service")]
    paths = _paths(service_req.dimension_filter.and_group.expressions[1])
    assert ("pagePath", ["/service"]) in paths and ("pagePath", "/service/") in paths
    assert st["segments"]["service"]["values"]["2026-09-28"] == 300


def test_legacy_comparison_only_in_transition_window():
    c = _RecClient([SimpleNamespace(dimension_values=[], metric_values=[SimpleNamespace(value="73")])])
    got = fetch_ga4.fetch_legacy_comparison(GA4_CFG, PERIOD, c)
    assert got["service"] == {"legacy_rule": "/service/", "current": 73}
    assert "b2b_contact" not in got
    late = {**PERIOD, "report_week_start": "2026-12-14", "report_week_end": "2026-12-20"}
    assert fetch_ga4.fetch_legacy_comparison(GA4_CFG, late, _RecClient()) is None


def test_rule_change_note_and_legacy_diff_are_shown():
    access = build_access_summary(_bundle(), THRESHOLDS)
    access["legacy_comparison"] = {"document_request": {"legacy_rule": "/download", "current": 164}}
    su = view_model._build_access(access, [], "2026-11-13")
    assert "11/16配信分までのレポートの値とは一致しない" in su["rule_notes"][0]
    assert su["rule_notes"][1] == "集計条件の変更による差（今週の値。旧条件の部分一致→新条件）: 資料請求ページ到達（一般） 164→154人。"


# ── ① 冒頭のサマリー（6カード） ─────────────────────────────────

def test_summary_tiles_reuse_detail_segments():
    su = _vm_site_usage()
    keys = [[s["key"] for s in row] for row in su["summary_tiles"]]
    assert keys == [["site_wide"], ["contact", "document_request"]]  # テスト用データにある分だけ
    by_key = {s["key"]: s for s in su["segments"]}
    for row in su["summary_tiles"]:
        for s in row:
            assert s is by_key[s["key"]]  # 同じオブジェクト（集計を重複させない・値が一致する）


def test_summary_tiles_render_same_numbers_as_table():
    from src_v2.report.render_html import _site_usage_html, _site_usage_v2_html
    from src_v2.report.render_md import _site_usage_section
    su = _vm_site_usage()
    head, table = _site_usage_html(su), _site_usage_v2_html(su)
    assert head.count('class="stat-tile"') == 3 and "<table" not in head
    assert "stat-tile" not in table
    assert "3,741" in head and "+189.1%" in head and "3,741" in table and "+189.1%" in table
    assert "※異常値を含む" in head  # 資料請求（一般）は詳細の表と同じく前週比を出さない
    assert "| サイト全体 | 3,741 | +189.1% |" in _site_usage_section(su)
    assert "**サマリー" not in render_markdown_section(su)


def test_summary_and_detail_are_separate_sections():
    su = _vm_site_usage()
    inq = _inquiries({**BASE_ROW, **FORMS}, su)
    vm = {
        "header": {"periods": {"title": "t", "header_note": "n", "sections": {
            "site_usage": "（11/9(月)〜11/15(日)）", "inquiries": "", "seo": "", "aio": ""},
            "notes": {"site_usage": ["速報値の注記"], "seo": []}},
            "data_quality_label": "pass", "weeks_available": 1, "low_impressions_threshold": 50},
        "site_usage": su, "inquiries": inq, "executive_summary": {"blocks": ["x"], "narrative": None},
        "priority_markets": {"table": [], "groups": [], "seo_notes": [], "aio_notes": []},
        "appendix": {"data_quality": {}, "other_pages": [], "unknown_initiatives": [],
                     "initiatives": {"scheme": "v2", "items": [], "notes": []},
                     "landing_pages": {"top": [], "excluded": []}, "all_findings": []},
    }
    html, md = render_html(vm), render_markdown(vm)
    sections = html.split("<section")
    first = next(s for s in sections if "① サイト全体のアクセス状況" in s)
    detail = next(s for s in sections if "アクセスの詳細（11/9(月)〜11/15(日)）" in s)
    assert "stat-tile" in first and "到達ユーザー（指標" not in first
    assert "到達ユーザー（指標" in detail and "流入元別" in detail and "速報値の注記" in detail
    assert html.index("① サイト全体のアクセス状況") < html.index("アクセスの詳細") < html.index("② 問い合わせ実績")
    assert md.index("## ① サイト全体") < md.index("## アクセスの詳細") < md.index("## ② 問い合わせ実績")


def render_markdown_section(su):
    from src_v2.report.render_md import _site_usage_v2
    return _site_usage_v2(su)
