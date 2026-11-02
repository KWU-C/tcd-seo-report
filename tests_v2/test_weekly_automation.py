"""週次自動運用（2026-11-02）のテスト。

- 期間: レポート週（月〜日）、GSCの確定済み最新7日、旧方式との接続
- 問い合わせ: スプレッドシートの行の検査、未集計・不整合・取得失敗の表示、手入力の優先、旧方式の週の扱い
- 二重配信の防止（送信済み台帳）、通知、無人実行時の認可
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2 import auth, config_loader, fetch_inquiries, notify, pipeline
from src_v2.analysis import summary
from src_v2.report import send
from src_v2.report.render_html import render_html
from src_v2.report.render_md import render_markdown
from src_v2.report.view_model import build_view_model

from tests_v2.test_report import _THRESHOLDS, _analysis

_PIPELINE_CFG = {"gsc": {"data_cutoff": {"mode": "auto", "end_offset_days": 2, "offset_days": 3},
                         "report_period": {"length_days": 7}}}


# ── 期間 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("today", ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-08"])
def test_report_week_is_previous_monday_to_sunday_regardless_of_retry_day(today):
    week = pipeline.get_report_week(today=date.fromisoformat(today))
    assert (week["report_week_start"], week["report_week_end"]) == ("2026-10-26", "2026-11-01")
    assert (week["ga4_prv_start"], week["ga4_prv_end"]) == ("2026-10-19", "2026-10-25")


def test_report_week_must_start_on_monday():
    with pytest.raises(ValueError, match="月曜"):
        pipeline.get_report_week(week_start=date(2026, 10, 27))


def test_gsc_period_on_monday_morning_is_saturday_to_friday():
    """2026-11-02 09:50 の実測: firstIncompleteDate=10/31 → GSCは 10/24〜10/30（今朝送ったレポートと連続）。"""
    week = pipeline.get_report_week(week_start=date(2026, 10, 26))
    gsc = pipeline.get_gsc_period(week, date(2026, 10, 31), date(2026, 11, 1), _PIPELINE_CFG)
    assert (gsc["cur_start"], gsc["cur_end"], gsc["prv_start"], gsc["prv_end"]) == (
        "2026-10-24", "2026-10-30", "2026-10-17", "2026-10-23")
    assert gsc["gsc_shift_days"] == 0


def test_gsc_period_shifts_back_when_friday_is_not_final():
    week = pipeline.get_report_week(week_start=date(2026, 10, 26))
    gsc = pipeline.get_gsc_period(week, date(2026, 10, 29), date(2026, 11, 1), _PIPELINE_CFG)
    assert (gsc["cur_start"], gsc["cur_end"]) == ("2026-10-22", "2026-10-28")
    assert gsc["gsc_shift_days"] == 2


def test_gsc_period_does_not_move_later_when_rerun_midweek():
    """水曜の再試行で日曜まで確定していても、末日は金曜のまま（再試行日によって期間が変わらない）。"""
    week = pipeline.get_report_week(week_start=date(2026, 10, 26))
    gsc = pipeline.get_gsc_period(week, date(2026, 11, 3), date(2026, 11, 3), _PIPELINE_CFG)
    assert gsc["cur_end"] == "2026-10-30"


def test_gsc_period_fails_when_search_console_is_far_behind():
    week = pipeline.get_report_week(week_start=date(2026, 10, 26))
    with pytest.raises(pipeline.GscFetchError, match="大幅に遅れ"):
        pipeline.get_gsc_period(week, date(2026, 10, 23), date(2026, 11, 1), _PIPELINE_CFG)


def test_unfinished_week_is_rejected():
    from datetime import datetime
    with pytest.raises(ValueError, match="終わっていない"):
        pipeline.determine_period(None, _PIPELINE_CFG, week_start=date(2026, 11, 2),
                                  now=datetime(2026, 11, 8, 23, 0, tzinfo=pipeline.JST))


def test_gsc_end_uses_last_date_when_everything_is_final():
    assert pipeline.compute_gsc_end(date(2026, 10, 30), None, date(2026, 10, 29)) == date(2026, 10, 29)


def test_gsc_end_fails_without_any_date_information():
    with pytest.raises(pipeline.GscFetchError):
        pipeline.compute_gsc_end(date(2026, 10, 30), None, None)


def _write_bundle(root: Path, folder: str, period: dict, ga4: dict | None = None):
    d = root / folder
    d.mkdir(parents=True)
    (d / "normalized.json").write_text(json.dumps({"period": period, "ga4": ga4 or {}}), encoding="utf-8")
    return d


def test_previous_gsc_week_is_found_in_legacy_folder(tmp_path):
    legacy = _write_bundle(tmp_path, "2026-10-24_2026-10-30", {"cur_start": "2026-10-24", "cur_end": "2026-10-30"})
    assert summary._find_dir_by_gsc_period(tmp_path, "2026-10-24", "2026-10-30") == legacy


def test_previous_gsc_week_is_found_in_new_folder_by_period(tmp_path):
    new = _write_bundle(tmp_path, "2026-10-26_2026-11-01",
                        {"cur_start": "2026-10-24", "cur_end": "2026-10-30", "report_week_start": "2026-10-26"})
    assert summary._find_dir_by_gsc_period(tmp_path, "2026-10-24", "2026-10-30") == new
    assert summary._find_dir_by_gsc_period(tmp_path, "2026-10-17", "2026-10-23") is None


def test_ga4_revision_compares_first_value_with_refetched_value(tmp_path):
    _write_bundle(tmp_path, "2026-10-26_2026-11-01", {"report_week_start": "2026-10-26"},
                  ga4={"site_wide": {"label": "サイト全体", "current": 1108, "comparison": 928}})
    bundle = {"period": {"ga4_prv_start": "2026-10-26", "ga4_prv_end": "2026-11-01"},
              "ga4": {"site_wide": {"label": "サイト全体", "current": 1200, "comparison": 1120}}}
    rev = summary._ga4_revision(bundle, tmp_path)
    assert rev["segments"]["site_wide"] == {"label": "サイト全体", "first": 1108, "refetched": 1120, "delta_pct": 1.1}


def test_ga4_revision_is_none_for_first_week_after_migration(tmp_path):
    bundle = {"period": {"ga4_prv_start": "2026-10-26", "ga4_prv_end": "2026-11-01"}, "ga4": {}}
    assert summary._ga4_revision(bundle, tmp_path) is None


# ── 問い合わせ: スプレッドシートの行 ───────────────────────────────

_HEADER = list(fetch_inquiries.COLUMNS)


def _sheet_row(start, end, status="確定", general=10, case=1, sales=7, other=2, review=0, recruiting=1, **extra):
    values = {"week_start": start, "week_end": end, "status": status, "general_total": general,
              "project_consultation": case, "sales_solicitation": sales, "other": other,
              "other_needs_review": review, "recruiting_form": recruiting, "excluded_replies": "",
              "confirmed_by": "Dot", "confirmed_at": "2026-11-09 09:20", "note": "", **extra}
    return [values[c] for c in _HEADER]


def test_confirmed_row_is_parsed_with_integer_counts():
    rows = fetch_inquiries.parse_rows([_HEADER, _sheet_row("2026-11-02", "2026-11-08", general=10.0)])
    assert rows[0]["state"] == fetch_inquiries.ROW_CONFIRMED
    assert rows[0]["general_total"] == 10 and isinstance(rows[0]["general_total"], int)


def test_in_progress_row_has_no_counts():
    rows = fetch_inquiries.parse_rows([_HEADER, _sheet_row("2026-11-02", "2026-11-08", status="集計中", general="")])
    assert rows[0]["state"] == fetch_inquiries.ROW_IN_PROGRESS


@pytest.mark.parametrize("kwargs, message", [
    ({"general": 11}, "一致しない"),
    ({"review": 3}, "other_needs_review"),
    ({"recruiting": -1}, "recruiting_form"),
    ({"recruiting": 1.5}, "recruiting_form"),
    ({"status": "済"}, "status"),
])
def test_invalid_rows_are_marked_not_raised(kwargs, message):
    rows = fetch_inquiries.parse_rows([_HEADER, _sheet_row("2026-11-02", "2026-11-08", **kwargs)])
    assert rows[0]["state"] == fetch_inquiries.ROW_INVALID
    assert any(message in e for e in rows[0]["errors"])


def test_week_must_be_monday_to_sunday():
    rows = fetch_inquiries.parse_rows([_HEADER, _sheet_row("2026-11-01", "2026-11-07")])
    assert rows[0]["state"] == fetch_inquiries.ROW_INVALID
    assert any("月曜" in e for e in rows[0]["errors"])


def test_duplicate_week_rows_are_all_invalid():
    rows = fetch_inquiries.parse_rows([_HEADER, _sheet_row("2026-11-02", "2026-11-08"),
                                       _sheet_row("2026-11-02", "2026-11-08", general=9, sales=6)])
    assert all(r["state"] == fetch_inquiries.ROW_INVALID for r in rows)


def test_columns_are_read_by_name_and_blank_rows_are_skipped():
    header = list(reversed(_HEADER))
    rows = fetch_inquiries.parse_rows([header, list(reversed(_sheet_row("2026-11-02", "2026-11-08"))), ["", ""]])
    assert len(rows) == 1 and rows[0]["project_consultation"] == 1


def test_snapshot_not_configured_without_sheet_id(monkeypatch):
    monkeypatch.delenv("INQUIRY_SHEET_ID", raising=False)
    snap = fetch_inquiries.fetch_snapshot({"source": "sheets", "sheet_id_env": "INQUIRY_SHEET_ID"})
    assert snap["status"] == fetch_inquiries.SNAPSHOT_NOT_CONFIGURED and snap["rows"] == []


def test_snapshot_fetch_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("INQUIRY_SHEET_ID", "dummy")

    def broken(*_args):
        raise auth.AuthorizationRequired("token_sheets.pickle が無い")

    snap = fetch_inquiries.fetch_snapshot({"source": "sheets", "sheet_id_env": "INQUIRY_SHEET_ID"}, fetcher=broken)
    assert snap["status"] == fetch_inquiries.SNAPSHOT_FETCH_FAILED
    assert "token_sheets.pickle" in snap["error"]


# ── 問い合わせ: inquiries.yaml の集計方式 ─────────────────────────────

def _yaml_week(start, end, scheme=None, general=10, case=0, sales=8, other=2):
    w = {"start": start, "end": end, "general_total": general, "project_consultation": case,
         "sales_solicitation": sales, "other": other, "other_needs_review": 0, "recruiting_form": 1}
    if scheme:
        w["scheme"] = scheme
    return w


def _write_yaml(tmp_path, weeks):
    path = tmp_path / "inquiries.yaml"
    path.write_text(yaml.safe_dump({"weeks": weeks}, allow_unicode=True), encoding="utf-8")
    return path


def test_repository_legacy_week_is_kept_as_legacy():
    legacy = next(w for w in config_loader.load_inquiries() if w["start"] == "2026-10-18")
    assert legacy["scheme"] == config_loader.INQUIRY_SCHEME_LEGACY
    assert (legacy["general_total"], legacy["sales_solicitation"]) == (10, 8)


def test_legacy_and_weekly_schemes_may_overlap(tmp_path):
    weeks = config_loader.load_inquiries(_write_yaml(tmp_path, [
        _yaml_week("2026-10-18", "2026-10-24", scheme="legacy_sun_sat"), _yaml_week("2026-10-19", "2026-10-25")]))
    assert [w["scheme"] for w in weeks] == ["legacy_sun_sat", "weekly_mon_sun"]


def test_weekly_scheme_must_start_on_monday(tmp_path):
    with pytest.raises(config_loader.InquiriesConfigError, match="月曜"):
        config_loader.load_inquiries(_write_yaml(tmp_path, [_yaml_week("2026-10-18", "2026-10-24", scheme="weekly_mon_sun")]))


# ── 問い合わせ: 表示 ─────────────────────────────────────────────

_WEEKLY_PERIOD = {
    "cur_start": "2026-10-31", "cur_end": "2026-11-06", "prv_start": "2026-10-24", "prv_end": "2026-10-30",
    "report_week_start": "2026-11-02", "report_week_end": "2026-11-08",
    "ga4_prv_start": "2026-10-26", "ga4_prv_end": "2026-11-01",
    "gsc_shift_days": 0, "aio_observed_at": "2026-11-09T10:01:12+09:00", "period_scheme": "weekly_v3",
}


def _snapshot(rows, status="ok", error=None):
    return {"source": "sheets", "status": status, "error": error,
            "rows": fetch_inquiries.parse_rows([_HEADER, *rows]) if rows else []}


def _weekly_vm(snapshot, manual=None, period=None):
    analysis = _analysis([])
    analysis["period"] = period or _WEEKLY_PERIOD
    analysis["inquiries_snapshot"] = snapshot
    return build_view_model(analysis, thresholds_cfg=_THRESHOLDS, notes_cfg=[], inquiries_cfg=manual or [])


def test_confirmed_sheet_week_is_shown():
    inq = _weekly_vm(_snapshot([_sheet_row("2026-11-02", "2026-11-08")]))["inquiries"]
    assert inq["status"] == "confirmed_sheet" and inq["entered"]
    assert {r["key"]: r["current"] for r in inq["rows"]}["project_consultation"] == 1


@pytest.mark.parametrize("snapshot, status, phrase", [
    (_snapshot([]), "not_counted", "Dotの集計が未完了"),
    (_snapshot([_sheet_row("2026-11-02", "2026-11-08", status="集計中", general="")]), "not_counted", "Dotの集計が未完了"),
    (_snapshot([_sheet_row("2026-11-02", "2026-11-08", general=99)]), "invalid", "不整合"),
    (_snapshot([], status="fetch_failed", error="HttpError 403"), "fetch_failed", "取得できなかった"),
    (_snapshot([], status="not_configured", error="未設定"), "not_configured", "未設定"),
    (None, "not_configured", "未設定"),
])
def test_uncounted_week_is_never_zero(snapshot, status, phrase):
    vm = _weekly_vm(snapshot)
    inq = vm["inquiries"]
    assert inq["status"] == status and not inq["entered"]
    assert phrase in inq["status_label"] and "0件ではない" in inq["status_label"]
    assert all(r["current"] is None for r in inq["rows"])
    md = render_markdown(vm)
    assert "| 一般問い合わせ | 未集計 |" in md and "| 一般問い合わせ | 0 |" not in md
    assert "未集計" in render_html(vm)


def test_manual_entry_overrides_sheet_and_warns_when_different():
    entry = {**_yaml_week("2026-11-02", "2026-11-08", general=10, case=2, sales=6), "scheme": "weekly_mon_sun"}
    inq = _weekly_vm(_snapshot([_sheet_row("2026-11-02", "2026-11-08")]), manual=[entry])["inquiries"]
    assert inq["status"] == "confirmed_manual"
    assert {r["key"]: r["current"] for r in inq["rows"]}["project_consultation"] == 2
    assert any("手入力" in w for w in inq["warnings"])


def test_legacy_week_is_not_used_for_weekly_cumulative():
    legacy = {**_yaml_week("2026-10-18", "2026-10-24"), "scheme": "legacy_sun_sat"}
    rows = [_sheet_row(s, e) for s, e in [("2026-10-19", "2026-10-25"), ("2026-10-26", "2026-11-01"),
                                          ("2026-11-02", "2026-11-08")]]
    inq = _weekly_vm(_snapshot(rows), manual=[legacy])["inquiries"]
    assert inq["cumulative"][4]["entered_weeks"] == 3 and not inq["cumulative"][4]["complete"]
    assert "未確定（4週中3週集計済み）" in render_markdown(_weekly_vm(_snapshot(rows), manual=[legacy]))


def test_cumulative_is_shown_when_four_consecutive_weeks_are_confirmed():
    rows = [_sheet_row(s, e) for s, e in [("2026-10-12", "2026-10-18"), ("2026-10-19", "2026-10-25"),
                                          ("2026-10-26", "2026-11-01"), ("2026-11-02", "2026-11-08")]]
    inq = _weekly_vm(_snapshot(rows))["inquiries"]
    assert inq["cumulative"][4]["complete"]
    assert {r["key"]: r["cumulative_4w"] for r in inq["rows"]}["project_consultation"] == 4


def test_weekly_header_states_each_period():
    vm = _weekly_vm(_snapshot([]))
    md = render_markdown(vm)
    assert "# TCD週次SEO/AIOレポート（2026-11-02 〜 2026-11-08）" in md
    assert "アクセス・問い合わせ: 11/2(月)〜11/8(日)" in md
    assert "検索順位: 10/31(土)〜11/6(金)（Search Consoleの確定済み最新7日）" in md
    assert "AIO: 11/9(月) 10:01 観測" in md
    assert "## ② 問い合わせ実績（11/2(月)〜11/8(日)）" in md
    assert "速報値" in md
    assert vm["header"]["periods"]["folder"] == "2026-11-02_2026-11-08"


def test_gsc_shift_is_noted_in_report():
    vm = _weekly_vm(_snapshot([]), period={**_WEEKLY_PERIOD, "gsc_shift_days": 1,
                                          "cur_start": "2026-10-30", "cur_end": "2026-11-05"})
    assert "通常より1日前までの期間" in render_markdown(vm)


def test_legacy_analysis_keeps_legacy_header():
    analysis = _analysis([])
    analysis["period"] = {"cur_start": "2026-10-24", "cur_end": "2026-10-30", "prv_start": "2026-10-17", "prv_end": "2026-10-23"}
    vm = build_view_model(analysis, thresholds_cfg=_THRESHOLDS, notes_cfg=[], inquiries_cfg=[])
    md = render_markdown(vm)
    assert "対象期間: 2026-10-24〜2026-10-30（データ確定基準: 実行日-3日オフセット）" in md
    assert "| 一般問い合わせ | 未入力 |" in md
    assert vm["header"]["periods"]["folder"] == "2026-10-24_2026-10-30"


# ── 二重配信の防止 ───────────────────────────────────────────────

class _FakeGmail:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sent.append(body)
        return self

    def execute(self):
        return {"id": f"msg{len(self.sent)}"}


def _report_dir(tmp_path):
    d = tmp_path / "reports" / "2026-11-02_2026-11-08"
    d.mkdir(parents=True)
    (d / "report.html").write_text("<html></html>", encoding="utf-8")
    (d / "report.json").write_text(json.dumps({"header": {"periods": {"title": "2026-11-02 〜 2026-11-08"}}}), encoding="utf-8")
    return d


def test_send_records_ledger_and_blocks_second_send(tmp_path):
    d, ledger, gmail = _report_dir(tmp_path), tmp_path / "sent", _FakeGmail()
    assert send.send_report(d, "unei@tcd.jp", ledger_dir=ledger, gmail_service=gmail) == "msg1"
    record = json.loads((ledger / "2026-11-02_2026-11-08.json").read_text(encoding="utf-8"))["sends"][0]
    assert record["to"] == "unei@tcd.jp" and record["message_id"] == "msg1"
    assert "2026-11-02 〜 2026-11-08" in record["subject"]
    with pytest.raises(send.AlreadySentError):
        send.send_report(d, "UNEI@tcd.jp", ledger_dir=ledger, gmail_service=gmail)
    assert len(gmail.sent) == 1


def test_force_resend_and_other_recipient_are_allowed(tmp_path):
    d, ledger, gmail = _report_dir(tmp_path), tmp_path / "sent", _FakeGmail()
    send.send_report(d, "unei@tcd.jp", ledger_dir=ledger, gmail_service=gmail)
    send.send_report(d, "kawauchi@tcd.jp", ledger_dir=ledger, gmail_service=gmail)
    send.send_report(d, "unei@tcd.jp", ledger_dir=ledger, gmail_service=gmail, force_resend=True)
    assert len(gmail.sent) == 3
    assert len(send.read_ledger(d, ledger)["sends"]) == 3


def test_send_cli_requires_report_dir(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["send", "--to", "unei@tcd.jp"])
    with pytest.raises(SystemExit):
        send.main()


# ── 通知 ────────────────────────────────────────────────────────

def test_review_items_cover_uncounted_inquiries_and_gsc_shift(tmp_path):
    vm = _weekly_vm(_snapshot([], status="fetch_failed", error="HttpError 403"),
                    period={**_WEEKLY_PERIOD, "gsc_shift_days": 1})
    d = tmp_path / "r"
    d.mkdir()
    (d / "report.json").write_text(json.dumps(vm, ensure_ascii=False), encoding="utf-8")
    items = notify.collect_review_items(d)
    assert any("取得できなかった" in i for i in items)
    assert any("HttpError 403" in i for i in items)
    assert any("1日前" in i for i in items)


def test_failure_message_names_step_and_manual_retry(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("line1\nGscFetchError: boom\n", encoding="utf-8")
    subject, body = notify.failure_message("2026-11-02", "データ取得・検証", log, "本日 11:00")
    assert "2026-11-02" in subject and "データ取得・検証" in subject
    assert "scripts/weekly_report.sh --week 2026-11-02" in body and "GscFetchError: boom" in body


def test_failure_notification_is_sent_once_per_week_and_step(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "NOTIFIED_DIR", tmp_path / "notified")
    sent = []
    monkeypatch.setattr(notify, "send_text_email", lambda to, s, b: sent.append(s) or True)
    monkeypatch.setattr(notify, "_mac_notify", lambda *a: None)
    argv = ["notify", "--kind", "failure", "--week", "2026-11-02", "--to", "kawauchi@tcd.jp", "--step", "送信"]
    monkeypatch.setattr(sys, "argv", argv)
    notify.main()
    notify.main()
    assert len(sent) == 1


# ── 無人実行時の認可 ─────────────────────────────────────────────

def test_sheets_auth_never_opens_browser_without_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "SHEETS_TOKEN_PATH", tmp_path / "token_sheets.pickle")
    with pytest.raises(auth.AuthorizationRequired):
        auth.get_sheets_credentials()


def test_noninteractive_env_blocks_browser_flow(monkeypatch):
    monkeypatch.setenv("TCD_NONINTERACTIVE", "1")
    assert auth._interactive_allowed() is False
