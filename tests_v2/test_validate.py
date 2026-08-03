import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2 import validate
from src_v2.schema import DataQualityError


def test_validate_period_ok():
    errors = validate.validate_period("2026-07-20", "2026-07-26", "2026-07-13", "2026-07-19")
    assert errors == []


def test_validate_period_not_7_days():
    errors = validate.validate_period("2026-07-20", "2026-07-25", "2026-07-13", "2026-07-19")
    assert any("7日ではない" in e for e in errors)


def test_validate_period_not_contiguous():
    errors = validate.validate_period("2026-07-20", "2026-07-26", "2026-07-12", "2026-07-18")
    assert any("連続していない" in e for e in errors)


def _raw(dimensions, rows):
    return {"dimensions": dimensions, "response": {"rows": rows}}


def test_validate_gsc_period_empty_current_is_error():
    raw_by_dim = {
        "date_query": _raw(["date", "query"], []),
        "date_page": _raw(["date", "page"], [{"keys": ["2026-07-20", "https://tcd.jp/"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}]),
        "date_page_query": _raw(["date", "page", "query"], [{"keys": ["2026-07-20", "https://tcd.jp/", "q"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}]),
    }
    errors, warnings = validate.validate_gsc_period(raw_by_dim, "今週")
    assert any("date_query" in e for e in errors)


def test_validate_gsc_period_empty_comparison_is_warning():
    raw_by_dim = {
        "date_query": _raw(["date", "query"], []),
        "date_page": _raw(["date", "page"], [{"keys": ["2026-07-13", "https://tcd.jp/"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}]),
        "date_page_query": _raw(["date", "page", "query"], [{"keys": ["2026-07-13", "https://tcd.jp/", "q"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}]),
    }
    errors, warnings = validate.validate_gsc_period(raw_by_dim, "比較週")
    assert errors == []
    assert any("date_query" in w for w in warnings)


def test_validate_gsc_period_duplicate_keys_is_error():
    dup_row = {"keys": ["2026-07-20", "q"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}
    raw_by_dim = {
        "date_query": _raw(["date", "query"], [dup_row, dup_row]),
        "date_page": _raw(["date", "page"], [{"keys": ["2026-07-20", "https://tcd.jp/"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}]),
        "date_page_query": _raw(["date", "page", "query"], [{"keys": ["2026-07-20", "https://tcd.jp/", "q"], "clicks": 1, "impressions": 1, "ctr": 1.0, "position": 1.0}]),
    }
    errors, warnings = validate.validate_gsc_period(raw_by_dim, "今週")
    assert any("重複キー" in e for e in errors)


def test_validate_aio_not_run_is_warning_not_error():
    records = [
        {"query": "ブランディング 会社 東京", "acquisition_status": "not_run"},
        {"query": "ブランディング 会社 大阪", "acquisition_status": "success"},
    ]
    errors, warnings, meta = validate.validate_aio(records, ["ブランディング 会社 東京", "ブランディング 会社 大阪"])
    assert errors == []
    assert len(warnings) == 1
    assert meta["aio_success_rate"] == 0.5


def test_run_validation_raises_on_fail(tmp_path):
    gsc_current = {
        "date_query": _raw(["date", "query"], []),
        "date_page": _raw(["date", "page"], []),
        "date_page_query": _raw(["date", "page", "query"], []),
    }
    gsc_comparison = gsc_current
    period = {"cur_start": "2026-07-20", "cur_end": "2026-07-26", "prv_start": "2026-07-13", "prv_end": "2026-07-19"}

    with pytest.raises(DataQualityError) as exc_info:
        validate.run_validation(
            gsc_current, gsc_comparison, [], [], period,
            manifest_path=tmp_path / "manifest.json",
        )
    assert exc_info.value.report["status"] == "fail"
