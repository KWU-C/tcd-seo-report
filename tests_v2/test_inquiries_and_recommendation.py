"""② 問い合わせ実績（config_v2/inquiries.yaml）と、⑤ AIO引用状況の「TCD推薦」列のテスト。"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aio_daily_monitor as monitor
from src_v2 import config_loader, normalize
from src_v2.report.render_html import render_html
from src_v2.report.render_md import render_markdown
from src_v2.report.view_model import build_view_model

from tests_v2.test_report import _THRESHOLDS, _analysis, _keyword_finding, _row


def _week(start, end, general=10, case=0, sales=8, other=2, review=0, recruiting=1):
    return {"start": start, "end": end, "general_total": general, "project_consultation": case,
            "sales_solicitation": sales, "other": other, "other_needs_review": review, "recruiting_form": recruiting}


def _write(tmp_path, weeks):
    path = tmp_path / "inquiries.yaml"
    path.write_text(yaml.safe_dump({"weeks": weeks}, allow_unicode=True), encoding="utf-8")
    return path


# ── 入力チェック（config_loader.load_inquiries） ─────────────────────

def test_repository_inquiries_file_is_valid_and_has_initial_week():
    weeks = config_loader.load_inquiries()
    first = next(w for w in weeks if w["start"] == "2026-10-18")
    assert (first["end"], first["general_total"], first["project_consultation"], first["sales_solicitation"],
            first["other"], first["recruiting_form"]) == ("2026-10-24", 10, 0, 8, 2, 1)


def test_missing_file_means_no_entries(tmp_path):
    assert config_loader.load_inquiries(tmp_path / "none.yaml") == []


def test_breakdown_must_match_total(tmp_path):
    path = _write(tmp_path, [_week("2026-10-18", "2026-10-24", general=10, case=1, sales=8, other=2)])
    with pytest.raises(config_loader.InquiriesConfigError, match="一致しない"):
        config_loader.load_inquiries(path)


def test_missing_count_is_an_error_not_zero(tmp_path):
    week = _week("2026-10-18", "2026-10-24")
    del week["project_consultation"]
    with pytest.raises(config_loader.InquiriesConfigError, match="project_consultation"):
        config_loader.load_inquiries(_write(tmp_path, [week]))


@pytest.mark.parametrize("value", [-1, 1.5, "3", True, None])
def test_counts_must_be_non_negative_integers(tmp_path, value):
    week = _week("2026-10-18", "2026-10-24")
    week["recruiting_form"] = value
    with pytest.raises(config_loader.InquiriesConfigError):
        config_loader.load_inquiries(_write(tmp_path, [week]))


def test_week_must_be_seven_days(tmp_path):
    with pytest.raises(config_loader.InquiriesConfigError, match="7日間"):
        config_loader.load_inquiries(_write(tmp_path, [_week("2026-10-18", "2026-10-25")]))


def test_overlapping_weeks_are_rejected(tmp_path):
    path = _write(tmp_path, [_week("2026-10-18", "2026-10-24"), _week("2026-10-20", "2026-10-26")])
    with pytest.raises(config_loader.InquiriesConfigError, match="重複"):
        config_loader.load_inquiries(path)


def test_needs_review_cannot_exceed_other(tmp_path):
    with pytest.raises(config_loader.InquiriesConfigError, match="other_needs_review"):
        config_loader.load_inquiries(_write(tmp_path, [_week("2026-10-18", "2026-10-24", other=2, review=3)]))


# ── 表示（View Model・Markdown・HTML） ───────────────────────────────

_PERIOD = {"cur_start": "2026-10-18", "cur_end": "2026-10-24", "prv_start": "2026-10-11", "prv_end": "2026-10-17"}


def _vm(inquiries, findings=None):
    analysis = _analysis(findings or [])
    analysis["period"] = _PERIOD
    return build_view_model(analysis, thresholds_cfg=_THRESHOLDS, notes_cfg=[], inquiries_cfg=inquiries)


def _rows(vm):
    return {r["key"]: r for r in vm["inquiries"]["rows"]}


def test_entered_week_is_shown_with_its_counts():
    vm = _vm([_week("2026-10-18", "2026-10-24")])
    rows = _rows(vm)
    assert [rows[k]["current"] for k in ("general_total", "project_consultation", "sales_solicitation", "other", "recruiting_form")] == [10, 0, 8, 2, 1]
    md = render_markdown(vm)
    assert "| うち案件相談 | 0 | 未確定（4週中1週入力） |" in md
    assert "| 専用採用フォーム（別集計） | 1 | 未確定（4週中1週入力） |" in md


def test_unentered_week_is_not_shown_as_zero():
    vm = _vm([_week("2026-10-11", "2026-10-17")])  # 前週だけ入力済み
    assert vm["inquiries"]["entered"] is False
    assert all(r["current"] is None for r in vm["inquiries"]["rows"])
    md, html = render_markdown(vm), render_html(vm)
    assert "| 一般問い合わせ | 未入力 |" in md and "| うち案件相談 | 未入力 |" in md
    assert "当週の件数は未入力" in md
    assert "未入力" in html
    section = md[md.index("## ② 問い合わせ実績"):md.index("## ③ 今週の要点")]
    assert "| 0 |" not in section


def test_zero_count_week_is_distinguished_from_unentered():
    vm = _vm([_week("2026-10-18", "2026-10-24", general=0, case=0, sales=0, other=0, recruiting=0)])
    assert vm["inquiries"]["entered"] is True
    assert "| 一般問い合わせ | 0 |" in render_markdown(vm)


def test_four_week_total_is_confirmed_only_when_all_four_weeks_are_entered():
    weeks = [_week("2026-09-27", "2026-10-03", general=5, case=1, sales=3, other=1, recruiting=0),
             _week("2026-10-04", "2026-10-10", general=6, case=2, sales=4, other=0, recruiting=0),
             _week("2026-10-11", "2026-10-17", general=7, case=0, sales=6, other=1, recruiting=2),
             _week("2026-10-18", "2026-10-24")]
    vm = _vm(weeks)
    cum = vm["inquiries"]["cumulative"][4]
    assert cum["complete"] is True and cum["entered_weeks"] == 4
    rows = _rows(vm)
    assert rows["general_total"]["cumulative_4w"] == 28
    assert rows["project_consultation"]["cumulative_4w"] == 3
    assert rows["recruiting_form"]["cumulative_4w"] == 3
    assert "| うち案件相談 | 0 | 3 |" in render_markdown(vm)


def test_missing_week_in_the_middle_keeps_four_week_total_unconfirmed():
    weeks = [_week("2026-09-27", "2026-10-03"), _week("2026-10-11", "2026-10-17"), _week("2026-10-18", "2026-10-24")]
    vm = _vm(weeks)
    cum = vm["inquiries"]["cumulative"][4]
    assert cum["complete"] is False and cum["entered_weeks"] == 3
    assert all(r["cumulative_4w"] is None for r in vm["inquiries"]["rows"])
    assert "未確定（4週中3週入力）" in render_markdown(vm)


def test_entries_for_other_weekly_windows_are_not_counted():
    # 対象期間と曜日がずれた週（例: 10/19〜10/25）は当週・累計のどちらにも数えない
    vm = _vm([_week("2026-10-19", "2026-10-25")])
    assert vm["inquiries"]["entered"] is False
    assert vm["inquiries"]["cumulative"][4]["entered_weeks"] == 0


def test_twelve_week_case_total_needs_twelve_entered_weeks():
    from datetime import date, timedelta
    start = date(2026, 10, 18)
    weeks = [_week((start - timedelta(days=7 * k)).isoformat(), (start - timedelta(days=7 * k - 6)).isoformat(),
                   general=3, case=1, sales=2, other=0, recruiting=0) for k in range(12)]
    vm = _vm(weeks)
    assert vm["inquiries"]["cumulative"][12]["complete"] is True
    assert _rows(vm)["project_consultation"]["cumulative_12w"] == 12
    assert "直近12週累計の案件相談: 12" in render_markdown(vm)

    vm = _vm(weeks[:11])
    assert "直近12週累計の案件相談: 未確定（12週中11週入力）" in render_markdown(vm)


def test_needs_review_count_is_noted():
    vm = _vm([_week("2026-10-18", "2026-10-24", other=2, review=1)])
    assert "その他のうち要確認: 1件" in render_markdown(vm)


def test_inquiries_section_is_between_access_and_highlights_in_html():
    html = render_html(_vm([_week("2026-10-18", "2026-10-24")]))
    assert html.index("① サイト全体のアクセス状況") < html.index("② 問い合わせ実績") < html.index("③ 今週の要点")
    assert "<strong>うち案件相談</strong>" in html


# ── TCD推薦（週次の正規化） ─────────────────────────────────────────

RULES = monitor.build_judgement_rules({
    "recommendation": {"known_company_names": ["パラドックス", "セブンデックス", "EIGHT BRANDING DESIGN"]},
})
LIST_WITH_TCD = ("東京のおすすめブランディング会社\n"
                 "株式会社TCD（中央区銀座）：50年以上の実績を持ち、戦略からデザイン、社内浸透までトータルでサポート。\n"
                 "株式会社パラドックス：企業の「志」を軸にしたブランド戦略。")
LIST_WITHOUT_TCD = ("東京のおすすめブランディング会社\n"
                    "EIGHT BRANDING DESIGN：経営戦略からロゴ、パッケージまで一貫して支援。\n"
                    "株式会社パラドックス：企業の「志」を軸にしたブランド戦略。\n"
                    "もしよろしければ、以下を教えていただけますか？")
INTRO_ONLY = "東京でおすすめの主なブランディング会社は以下の通りです。\nもしよろしければ、以下を教えていただけますか？"


def _record(text_blocks=None, query="ブランディング 会社 東京", status="success", ai_overview=None):
    if ai_overview is None:
        ai_overview = {"text_blocks": [{"snippet": line} for line in (text_blocks or "").splitlines()],
                       "references": [{"link": "https://prdx.co.jp/tokyo/", "source": "株式会社パラドックス"}]}
    return {"observed_at": "2026-10-27T00:27:17Z", "query": query, "acquisition_status": status,
            "raw_response": {"search_result": {"ai_overview": ai_overview}, "ai_overview_result": None} if status == "success" else None,
            "error_message": None if status == "success" else "timeout"}


def _obs(record):
    return normalize.build_aio_observation(record, recommendation_rules=RULES)


def test_recommended_when_tcd_is_in_company_list():
    obs = _obs(_record(LIST_WITH_TCD))
    assert (obs["recommendation_status"], obs["tcd_recommended"]) == ("recommended", True)
    assert obs["tcd_cited"] is False  # 引用元にTCDはない（推薦と引用は独立）


def test_not_recommended_only_when_complete_list_is_observed():
    obs = _obs(_record(LIST_WITHOUT_TCD))
    assert (obs["recommendation_status"], obs["tcd_recommended"]) == ("not_recommended", False)


def test_list_heading_without_items_is_undeterminable():
    obs = _obs(_record(INTRO_ONLY))
    assert (obs["recommendation_status"], obs["tcd_recommended"]) == ("undeterminable", None)


def test_page_token_only_is_undeterminable():
    obs = _obs(_record(ai_overview={"page_token": "tok", "serpapi_link": "https://serpapi.com/x"}))
    assert obs["recommendation_status"] == "undeterminable"
    assert obs["recommendation_reason"] == "aio_content_unavailable"
    assert obs["tcd_recommended"] is None


def test_acquisition_failure_is_undeterminable_not_absent():
    obs = _obs(_record(status="failed"))
    assert obs["recommendation_status"] == "undeterminable"
    assert obs["recommendation_reason"] == "acquisition_failed"
    assert obs["tcd_recommended"] is None


def test_non_company_query_is_not_a_recommendation_target():
    obs = _obs(_record(LIST_WITH_TCD, query="インナーブランディング"))
    assert obs["recommendation_status"] == "not_applicable"
    assert obs["tcd_recommended"] is None


def test_not_generated_has_no_recommendation():
    obs = _obs({"observed_at": "x", "query": "ブランディング 会社 東京", "acquisition_status": "not_generated",
                "raw_response": {"search_result": {}, "ai_overview_result": None}, "error_message": None})
    assert obs["recommendation_status"] == "no_aio"


def test_weekly_and_daily_judgements_agree():
    for text in (LIST_WITH_TCD, LIST_WITHOUT_TCD, INTRO_ONLY):
        weekly = _obs(_record(text))
        daily = monitor.evaluate_observation("ブランディング 会社 東京", "OK_WITH_AIO", True, text,
                                             ["https://prdx.co.jp/tokyo/"], ["prdx.co.jp"], ["株式会社パラドックス"], RULES)
        assert weekly["recommendation_status"] == daily["recommendation_status"]


# ── TCD推薦（表示） ──────────────────────────────────────────────────

def _rec_finding(region, status, recommended):
    f = _keyword_finding(f"market-{region}", region, 9.0, 9.5, 120,
                         citations={"status": "extracted", "domains": ["a.com"]})
    f["evidence"]["aio"].update({"recommendation_status": status, "tcd_recommended": recommended})
    return f


@pytest.mark.parametrize("status,recommended,label", [
    ("recommended", True, "あり"),
    ("not_recommended", False, "なし"),
    ("undeterminable", None, "判定不可"),
    ("not_applicable", None, "対象外"),
])
def test_recommendation_label(status, recommended, label):
    vm = _vm([], [_rec_finding("tokyo", status, recommended)])
    assert _row(vm, "東京")["tcd_recommended_label"] == label


def test_analysis_without_recommendation_field_shows_dash():
    vm = _vm([], [_keyword_finding("market-tokyo", "tokyo", 9.0, 9.5, 120, citations={"status": "extracted", "domains": ["a.com"]})])
    assert _row(vm, "東京")["tcd_recommended_label"] == "—"


def test_recommendation_column_in_md_and_html_and_undeterminable_is_not_counted_as_absent():
    vm = _vm([], [_rec_finding("tokyo", "recommended", True), _rec_finding("osaka", "undeterminable", None)])
    md, html = render_markdown(vm), render_html(vm)
    assert "| キーワード | AIO生成 | TCD引用 | TCD推薦 | 主な引用元 |" in md
    assert "<th>TCD推薦</th>" in html
    highlight = next(b for b in vm["executive_summary"]["blocks"] if b.startswith(("AIO", "定点観測")))
    assert "東京でTCD推薦あり" in highlight
    assert "大阪は推薦を判定不可" in highlight
    assert "推薦なし" not in highlight
