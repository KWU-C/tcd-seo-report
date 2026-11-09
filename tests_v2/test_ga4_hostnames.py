"""GA4 の集計を本番ホスト名に絞る（2026-11-16週〜）。"""
import json
from types import SimpleNamespace

from src_v2 import fetch_ga4
from src_v2.analysis import summary
from src_v2.report import view_model

HOSTS = ["tcd.jp", "www.tcd.jp"]


class _Client:
    def __init__(self):
        self.requests = []

    def run_report(self, request):
        self.requests.append(request)
        return SimpleNamespace(rows=[SimpleNamespace(metric_values=[SimpleNamespace(value="10")])],
                               dimension_headers=[SimpleNamespace(name="sessionSourceMedium"), SimpleNamespace(name="dateRange")],
                               row_count=0)


def _host_values(expr):
    """FilterExpression から hostName の in_list の値を探す。"""
    if expr.filter.field_name == "hostName":
        return list(expr.filter.in_list_filter.values)
    for e in expr.and_group.expressions:
        if e.filter.field_name == "hostName":
            return list(e.filter.in_list_filter.values)
    return None


def test_active_users_filters_hostname_and_path():
    c = _Client()
    fetch_ga4._fetch_active_users(c, "1", "2026-11-09", "2026-11-15", "/contact", HOSTS)
    f = c.requests[0].dimension_filter
    assert _host_values(f) == HOSTS
    assert any(e.filter.field_name == "pagePathPlusQueryString" for e in f.and_group.expressions)


def test_active_users_site_wide_uses_hostname_only():
    c = _Client()
    fetch_ga4._fetch_active_users(c, "1", "2026-11-09", "2026-11-15", None, HOSTS)
    assert _host_values(c.requests[0].dimension_filter) == HOSTS


def test_no_hostnames_means_no_filter():
    c = _Client()
    fetch_ga4._fetch_active_users(c, "1", "2026-11-09", "2026-11-15", None, [])
    assert not c.requests[0].dimension_filter  # 変更前と同じ（絞らない）



def _write(root, folder, period, ga4):
    d = root / folder
    d.mkdir(parents=True)
    (d / "normalized.json").write_text(json.dumps({"period": period, "ga4": ga4}), encoding="utf-8")


PREV = {"report_week_start": "2026-11-02", "report_week_end": "2026-11-08", "cur_start": "2026-10-31", "cur_end": "2026-11-06"}


def test_revision_skipped_when_hostnames_changed(tmp_path):
    _write(tmp_path, "2026-11-02_2026-11-08", PREV, {"site_wide": {"label": "サイト全体", "current": 1503}})
    bundle = {"period": {"ga4_prv_start": "2026-11-02", "ga4_prv_end": "2026-11-08", "ga4_hostnames": HOSTS},
              "ga4": {"site_wide": {"label": "サイト全体", "current": 1400, "comparison": 1480}}}
    rev = summary._ga4_revision(bundle, tmp_path)
    assert rev["skipped"] == "hostnames_changed" and rev["segments"] == {}


def test_revision_compared_when_hostnames_same(tmp_path):
    _write(tmp_path, "2026-11-02_2026-11-08", {**PREV, "ga4_hostnames": HOSTS}, {"site_wide": {"label": "サイト全体", "current": 1480}})
    bundle = {"period": {"ga4_prv_start": "2026-11-02", "ga4_prv_end": "2026-11-08", "ga4_hostnames": HOSTS},
              "ga4": {"site_wide": {"label": "サイト全体", "current": 1400, "comparison": 1490}}}
    rev = summary._ga4_revision(bundle, tmp_path)
    assert rev["segments"]["site_wide"]["first"] == 1480


def test_report_notes_mention_hostnames():
    period = {"report_week_start": "2026-11-09", "report_week_end": "2026-11-15", "cur_start": "2026-11-07",
              "cur_end": "2026-11-13", "prv_start": "2026-10-31", "prv_end": "2026-11-06", "ga4_hostnames": HOSTS}
    rev = {"period": {"start": "2026-11-02", "end": "2026-11-08"}, "segments": {}, "skipped": "hostnames_changed"}
    p = view_model._build_periods(period, rev)
    assert any("tcd.jp・www.tcd.jp" in n for n in p["notes"]["site_usage"])
    lines = view_model._data_quality_extra_lines(p, {"status_label": "確定", "warnings": []})
    assert any("比べない" in l for l in lines)
