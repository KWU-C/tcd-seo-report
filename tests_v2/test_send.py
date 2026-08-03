import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2.report.send import _latest_report_dir, _subject_for


def test_latest_report_dir_picks_most_recent_by_name(tmp_path):
    for period in ["2026-07-06_2026-07-12", "2026-07-20_2026-07-26", "2026-07-13_2026-07-19"]:
        d = tmp_path / period
        d.mkdir()
        (d / "report.html").write_text("<html></html>", encoding="utf-8")

    result = _latest_report_dir(tmp_path)
    assert result.name == "2026-07-20_2026-07-26"


def test_latest_report_dir_raises_when_empty(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        _latest_report_dir(tmp_path)


def test_subject_for_uses_period_from_report_json(tmp_path):
    d = tmp_path / "2026-07-20_2026-07-26"
    d.mkdir()
    (d / "report.json").write_text(json.dumps({
        "header": {"period": {"cur_start": "2026-07-20", "cur_end": "2026-07-26"}}
    }), encoding="utf-8")

    subject = _subject_for(d)
    assert "2026-07-20" in subject
    assert "2026-07-26" in subject
    assert "[TEST]" not in subject


def test_subject_for_falls_back_to_dir_name_without_report_json(tmp_path):
    d = tmp_path / "2026-07-20_2026-07-26"
    d.mkdir()
    subject = _subject_for(d)
    assert "2026-07-20_2026-07-26" in subject
