"""Independent adapter invariants; fixtures contain synthetic metrics only."""
import copy
import json
from pathlib import Path
import pytest

from looker_sync.weekly import (HEADERS, LABELS, all_events, ingest, open_store,
                               parse_report, run, sheet_requests)


def report(verdict="比較対照と異なる減少（原因未確定）", generated="2026-11-23T01:00:00Z"):
    return {"schema_version": "1.0.0", "generated_at": generated,
            "header": {"data_quality_status": "pass", "period": {
                "cur_start": "2026-11-14", "cur_end": "2026-11-20",
                "prv_start": "2026-11-07", "prv_end": "2026-11-13"}},
            "appendix": {"all_findings": [], "initiatives": {"scheme": "v2", "items": [{
                "id": "initiative-synthetic", "name": "テスト施策", "verdict": verdict,
                "urls": ["https://example.com/test"], "implemented_at": "2026-10-19",
                "metrics_rows": [{"label": "表示回数", "pre": "10", "post": "11",
                                  "change": "+10%", "control_change": "+20%"}],
                "reasons": ["原因未確定"], "hypothesis": "テスト仮説", "overlaps": []}]}}}


def raw(d):
    return json.dumps(d, ensure_ascii=False).encode()


def test_old_new_layout_same_records_and_unchanged_original():
    d = report(); before = copy.deepcopy(d)
    r = parse_report(raw(d), "2026-11-23T02:00:00Z")[0]
    assert d == before
    assert r["source_item"]["verdict"] == d["appendix"]["initiatives"]["items"][0]["verdict"]
    assert r["row"][5] == "比較対照を下回る変化（原因未確定）"
    assert r["row"][6:9] == ["未確認", "未判断", "未記入｜人の評価・判断は別途記録"]
    d["layout"] = "content"; d["content"] = {"initiatives": {"rows": []}}
    assert parse_report(raw(d), "now")[0]["source_item"] == r["source_item"]


def test_same_report_idempotent_and_regeneration_append_only(tmp_path):
    db = open_store(tmp_path / "history.db")
    a = parse_report(raw(report()), "first")
    assert ingest(db, a) == 1
    assert ingest(db, parse_report(raw(report()), "second")) == 0
    b = parse_report(raw(report(generated="2026-11-23T03:00:00Z")), "third")
    assert ingest(db, b) == 1
    events = all_events(db)
    assert len(events) == 2 and events[0]["imported_at"] == "first"


def test_missing_url_is_not_inferred():
    d = report(); d["appendix"]["initiatives"]["items"][0]["urls"] = []
    r = parse_report(raw(d), "now")[0]
    assert r["row"][2] == "未設定（元週次出力に対象URLなし）"
    assert "数値の前後比較なし" not in r["row"][4]


@pytest.mark.parametrize("damage", ["bad_schema", "bad_quality", "bad_date", "duplicate", "missing_time", "bad_json"])
def test_invalid_input_fail_closed(damage):
    d = report()
    if damage == "bad_schema": d["schema_version"] = "future"
    if damage == "bad_quality": d["header"]["data_quality_status"] = "fail"
    if damage == "bad_date": d["header"]["period"]["cur_start"] = "invalid"
    if damage == "duplicate": d["appendix"]["initiatives"]["items"] *= 2
    if damage == "missing_time": del d["generated_at"]
    with pytest.raises((ValueError, KeyError)):
        parse_report(b"{" if damage == "bad_json" else raw(d), "now")


def test_request_never_updates_existing_rows_or_human_judgments():
    records = parse_report(raw(report()), "now")
    requests = sheet_requests(records, {"REV-I01", "HUMAN"}, 123, 28, 28, exemplar=1)
    assert requests[0]["appendDimension"]["length"] == 1
    update = requests[-1]["updateCells"]
    assert update["range"]["startRowIndex"] == 28
    assert update["range"]["endRowIndex"] == 29
    assert len(update["rows"][0]["values"]) == len(HEADERS)
    assert all(p["copyPaste"]["pasteType"] != "PASTE_NORMAL" for p in requests if "copyPaste" in p)
    assert sheet_requests(records, {records[0]["id"]}, 123, 29, 29) == []


def test_api_timeout_retries_by_remote_ids_preserves_human_review():
    records = parse_report(raw(report()), "now")
    remote = [{"id": "REV-I01", "human": "承認済"}]
    requests = sheet_requests(records, {r["id"] for r in remote}, 123, 2, 200)
    assert requests
    remote.append({"id": records[0]["id"], "human": "改善"})  # server committed, response lost
    assert not sheet_requests(records, {r["id"] for r in remote}, 123, 3, 200)
    assert remote[0]["human"] == "承認済" and remote[1]["human"] == "改善"


def test_failed_publication_does_not_change_source_or_lose_history(tmp_path):
    reports = tmp_path / "reports"; source = reports / "week" / "report.json"
    source.parent.mkdir(parents=True); source.write_bytes(raw(report())); before = source.read_bytes()
    class Fail:
        def publish(self, events): raise TimeoutError("not logged")
    state = tmp_path / "state"
    with pytest.raises(TimeoutError): run(reports, state, True, "book", Fail())
    assert source.read_bytes() == before
    assert json.loads((state / "status.json").read_text())["status"] == "failed"
    assert len(json.loads((state / "machine_history.json").read_text())) == 1
    assert run(reports, state)["new_events"] == 0


def test_no_report_does_not_report_success(tmp_path):
    assert run(tmp_path / "absent", tmp_path / "state")["status"] == "warning"


def test_independent_state_must_not_be_in_weekly_output(tmp_path):
    with pytest.raises(ValueError): run(tmp_path, tmp_path / "sync")


def test_existing_verdict_labels_match_weekly_code():
    # Presentation-only compatibility mapping, checked against current upstream constants.
    from src_v2.analysis.initiatives import LEGACY_VERDICTS
    assert LABELS == LEGACY_VERDICTS
