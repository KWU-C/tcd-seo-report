import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_v2.report.build import build_reports


def _write_analysis(tmp_path: Path) -> Path:
    analysis = {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-27T00:00:00Z",
        "period": {"cur_start": "2026-07-20", "cur_end": "2026-07-26", "prv_start": "2026-07-13", "prv_end": "2026-07-19"},
        "data_quality": {"status": "pass", "errors": [], "warnings": [], "aio_success_rate": 1.0},
        "data_summary": {
            "gsc": {"query_daily_rows": 1, "page_daily_rows": 1, "page_query_daily_rows": 1, "comparison_available": True},
            "aio": {"success": 1, "not_generated": 0, "not_observed": 0, "failed": 0, "not_run": 0},
        },
        "weeks_available": 1,
        "findings": [],
        "counts": {"total": 0, "by_severity": {}, "by_category": {}},
    }
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    return path


def test_build_reports_writes_all_formats_with_current_sections(tmp_path):
    analysis_path = _write_analysis(tmp_path)
    paths = build_reports(analysis_path, output_root=tmp_path / "reports_v2")
    assert set(paths) == {"json", "md", "html"}
    for fmt in ("md", "html"):
        out = paths[fmt].read_text(encoding="utf-8")
        for heading in ("① サイト全体のアクセス状況", "② 問い合わせ実績", "③ 今週の要点", "④ SEO検索順位", "⑤ AIO引用状況"):
            assert heading in out


def test_build_reports_with_narrative_file_overrides_summary(tmp_path):
    analysis_path = _write_analysis(tmp_path)
    narrative_path = tmp_path / "narrative.txt"
    narrative_path.write_text("今週は大きな変化はなく、データ蓄積を継続する段階です。", encoding="utf-8")

    paths = build_reports(analysis_path, output_root=tmp_path / "reports_v2", narrative_path=narrative_path)
    md = paths["md"].read_text(encoding="utf-8")
    html = paths["html"].read_text(encoding="utf-8")

    assert "今週は大きな変化はなく、データ蓄積を継続する段階です。" in md
    assert "今週は大きな変化はなく、データ蓄積を継続する段階です。" in html
    assert "③ 今週の要点" in md
