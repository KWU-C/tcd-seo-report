"""Append-only initiative history from completed report.json; no collection or scoring."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

HEADERS = ["評価ID", "施策名", "対象URL", "本番反映日", "観測事実", "機械判定",
           "人評価結果", "次の判断（人）", "判断根拠・確認者", "期待変化・評価指標",
           "評価期間・基準日", "根拠・関連ID", "対応状態", "更新方式", "14日評価予定", "28日評価予定"]
LABELS = {"比較対照と異なる減少（原因未確定）": "比較対照を下回る変化（原因未確定）",
          "比較対照と異なる増加（原因未確定）": "比較対照を上回る変化（原因未確定）",
          "指標で方向が異なる（原因未確定）": "指標で比較対照との差の向きが異なる（原因未確定）"}
IMPORT_VERSION = "weekly-output-v1"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_report(raw: bytes, imported_at: str):
    """Support both existing JSON layouts via their common detailed appendix.

    No text scraping, no invented URL, no absent-value-to-zero conversion.
    Exact source verdict/evidence is retained, display labels only are normalized.
    """
    report = json.loads(raw)
    generated = report["generated_at"]
    stamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("generated_at must include a timezone")
    if report.get("schema_version") != "1.0.0":
        raise ValueError("unsupported weekly schema_version")
    header = report["header"]
    if header.get("data_quality_status") not in ("pass", "warning"):
        raise ValueError("weekly quality is not pass/warning")
    period = header["period"]
    for key in ("cur_start", "cur_end", "prv_start", "prv_end"):
        date.fromisoformat(period[key])
    if not (period["prv_start"] <= period["prv_end"] < period["cur_start"] <= period["cur_end"]):
        raise ValueError("invalid or overlapping GSC periods")
    section = report["appendix"]["initiatives"]
    if section["scheme"] != "v2" or not isinstance(section["items"], list) or not section["items"]:
        raise ValueError("unsupported or empty initiative section")
    details = {f["id"]: f for f in report["appendix"].get("all_findings", [])
               if f.get("category") == "initiative"}
    digest = hashlib.sha256(raw).hexdigest()
    records, seen = [], set()
    for item in section["items"]:
        source_id = item["id"]
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            raise ValueError("missing or duplicate initiative ID")
        seen.add(source_id)
        for key in ("name", "verdict"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"initiative {key} missing")
        urls = item["urls"]
        if not isinstance(urls, list) or any(not isinstance(u, str) or not u.startswith("https://") for u in urls):
            raise ValueError("invalid initiative URLs")
        implemented = item.get("implemented_at")
        if implemented and implemented != "unknown":
            date.fromisoformat(implemented)
        record_id = "AUTO-" + hashlib.sha256((digest + "\n" + source_id).encode()).hexdigest()[:28]
        finding = details.get(source_id, {})
        evidence = finding.get("evidence", {})
        metrics = item.get("metrics_rows", [])
        observed = "／".join(f"{m['label']} {m.get('pre', '未取得')}→{m.get('post', '未取得')}"
                            f"（変化 {m.get('change', '未取得')}、比較対照 {m.get('control_change', '未取得')}）"
                            for m in metrics)
        if not observed:
            observed = "数値の前後比較なし｜" + "／".join(item.get("reasons", []))
        reason = "／".join(item.get("reasons", []))
        overlap = "・".join(item.get("overlaps", [])) or "元出力の記載なし"
        window = item.get("period_label") or "比較期間未設定"
        gsc_period = f"GSC {period['cur_start']}〜{period['cur_end']}／前期 {period['prv_start']}〜{period['prv_end']}"
        # Human-owned G:I are initialized for NEW records only. Existing rows are never rewritten.
        row = [record_id, item["name"], "\n".join(urls) or "未設定（元週次出力に対象URLなし）",
               implemented if implemented and implemented != "unknown" else "未確認",
               f"週次出力 {generated}｜取込 {imported_at}｜{observed}",
               LABELS.get(item["verdict"], item["verdict"]), "未確認", "未判断",
               "未記入｜人の評価・判断は別途記録", item.get("hypothesis") or "未取得",
               f"{window}｜{gsc_period}",
               f"{source_id}｜SHA256:{digest}｜機械の根拠:{reason}｜重複施策:{overlap}"
               f"｜元の提案:{item.get('recommendation') or '未取得'}",
               "週次出力参照（実装状態の正本は既存台帳）",
               f"独立取込 {IMPORT_VERSION}｜生成 {generated}｜取込 {imported_at}",
               "元週次出力の評価期間に従う（未評価を自動補完しない）",
               "元週次出力の評価期間に従う（未評価を自動補完しない）"]
        records.append({"id": record_id, "source_id": source_id, "source_sha256": digest,
                        "generated_at": generated, "imported_at": imported_at, "period": period,
                        "source_item": item, "source_finding": finding, "row": row,
                        "windows": evidence.get("windows"), "import_version": IMPORT_VERSION})
    return records


def open_store(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL)")
    return db


def ingest(db, records):
    before = db.total_changes
    with db:
        db.executemany("INSERT OR IGNORE INTO events VALUES (?, ?, ?)",
                       [(r["id"], r["generated_at"], canonical(r)) for r in records])
    return db.total_changes - before


def all_events(db):
    return [json.loads(r[0]) for r in db.execute("SELECT payload FROM events ORDER BY generated_at, id")]


def sheet_requests(records, existing_ids, sheet_id, next_row, grid_rows, exemplar=None, spreadsheet_id=None):
    """Pure request builder: append NEW rows, never write existing human cells.

    PASTE_FORMAT/DATA_VALIDATION only; no literal/formula/chip copying.
    next_row is obtained by inspecting the last nonempty row in A:P, not len(A:A).
    """
    pending = [r for r in records if r["id"] not in existing_ids]
    if not pending:
        return []
    end = next_row + len(pending)
    requests = []
    if end > grid_rows:
        requests.append({"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": end-grid_rows}})
    target = {"sheetId": sheet_id, "startRowIndex": next_row, "endRowIndex": end,
              "startColumnIndex": 0, "endColumnIndex": 16}
    if exemplar is not None:
        source = {"sheetId": sheet_id, "startRowIndex": exemplar, "endRowIndex": exemplar+1,
                  "startColumnIndex": 0, "endColumnIndex": 16}
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests.append({"copyPaste": {"source": source, "destination": target, "pasteType": paste_type}})
    rows = []
    for offset, record in enumerate(pending):
        cells = [{"userEnteredValue": {"stringValue": str(v)}} for v in record["row"]]
        # This existing column is URL-typed in Looker; keep a real ledger URL, not prose.
        cells[11] = {"userEnteredValue": {"stringValue":
                     f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}&range=A{next_row+offset+1}:P{next_row+offset+1}"
                     if spreadsheet_id else ""}}
        cells[5]["note"] = canonical({"source_id": record["source_id"], "source_sha256": record["source_sha256"],
                           "source_generated_at": record["generated_at"], "imported_at": record["imported_at"],
                           "source_item": record["source_item"], "windows": record["windows"]})
        rows.append({"values": cells})
    requests.append({"updateCells": {"range": target, "rows": rows, "fields": "userEnteredValue,note"}})
    return requests


class SheetsSink:
    """Uses an independently authorized ADC identity; never touches weekly OAuth tokens."""
    def __init__(self, spreadsheet_id):
        import google.auth
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=45))
        self.api = build("sheets", "v4", http=http, cache_discovery=False).spreadsheets()
        self.book = spreadsheet_id

    def publish(self, records):
        meta = self.api.get(spreadsheetId=self.book, fields="sheets(properties,basicFilter)").execute(num_retries=0)
        matches = [s for s in meta["sheets"] if s["properties"]["title"] == "施策評価"]
        if len(matches) != 1:
            raise ValueError("施策評価 sheet not uniquely found")
        sheet = matches[0]["properties"]
        grid_rows = sheet["gridProperties"]["rowCount"]
        values = self.api.values().get(spreadsheetId=self.book, range=f"'施策評価'!A1:P{grid_rows}",
                                       valueRenderOption="FORMATTED_VALUE").execute(num_retries=0).get("values", [])
        if not values or values[0] != HEADERS:
            raise ValueError("施策評価 headers changed; refuse to write")
        existing_ids = {r[0] for r in values[1:] if r and r[0]}
        requests = sheet_requests(records, existing_ids, sheet["sheetId"], len(values), grid_rows,
                                  exemplar=1 if len(values) > 1 else None, spreadsheet_id=self.book)
        if not requests:
            return 0
        basic_filter = matches[0].get("basicFilter")
        if basic_filter and basic_filter.get("range", {}).get("startRowIndex", 0) == 0:
            # Preserve criteria/sort specs while including appended rows in the existing filter.
            basic_filter = json.loads(json.dumps(basic_filter))
            basic_filter["range"]["endRowIndex"] = max(
                basic_filter["range"].get("endRowIndex", 0), requests[-1]["updateCells"]["range"]["endRowIndex"])
            requests.append({"setBasicFilter": {"filter": basic_filter}})
        # No automatic retry of writes: if response is lost, next run rereads remote IDs.
        self.api.batchUpdate(spreadsheetId=self.book, body={"requests": requests}).execute(num_retries=0)
        expected = {r["id"] for r in records}
        actual = self.api.values().get(spreadsheetId=self.book, range=f"'施策評価'!A1:A{max(grid_rows, len(values)+len(records))}").execute(num_retries=0)
        ids = {r[0] for r in actual.get("values", []) if r}
        if not expected.issubset(ids):
            raise RuntimeError("remote append verification failed")
        return len(expected-existing_ids)


@contextmanager
def exclusive_lock(path):
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def run(reports_root, state_dir, publish=False, spreadsheet_id=None, sink=None):
    reports_root, state_dir = Path(reports_root).resolve(), Path(state_dir).resolve()
    if reports_root == state_dir or reports_root in state_dir.parents or state_dir in reports_root.parents:
        raise ValueError("independent state must be outside reports_root")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_lock(state_dir / "sync.lock"):
        db = open_store(state_dir / "history.sqlite3")
        try:
            imported, errors, files = 0, [], sorted(reports_root.glob("*/report.json"))
            for path in files:
                # Independent lock + read twice: ignore concurrent/incomplete source writes, retry next run.
                try:
                    before = path.stat()
                    raw = path.read_bytes()
                    after = path.stat()
                    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                        raise ValueError("source changed while reading")
                    imported += ingest(db, parse_report(raw, utc_now()))
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    errors.append({"file": path.name, "folder": path.parent.name, "error_type": type(exc).__name__})
            if not files:
                errors.append({"error_type": "NoCompletedReport"})
            events = all_events(db)
            atomic_json(state_dir / "machine_history.json", events)
            published = 0
            if publish:
                if not spreadsheet_id:
                    raise ValueError("spreadsheet_id is required for publish")
                published = (sink or SheetsSink(spreadsheet_id)).publish(events)
                atomic_json(state_dir / "last_publication.json", {"checked_at": utc_now(),
                            "source_generated_at": max((r["generated_at"] for r in events), default=None),
                            "verified_event_ids": [r["id"] for r in events], "new_rows": published})
            status = {"checked_at": utc_now(), "status": "warning" if errors else "ok", "scanned_reports": len(files),
                      "new_events": imported, "history_events": len(events), "published": published,
                      "last_source_generated_at": max((r["generated_at"] for r in events), default=None),
                      "publish_requested": publish, "errors": errors}
            atomic_json(state_dir / "status.json", status)
            return status
        except Exception as exc:
            atomic_json(state_dir / "status.json", {"checked_at": utc_now(), "status": "failed",
                        "publish_requested": publish, "error_type": type(exc).__name__})
            raise
        finally:
            db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reports-root", type=Path, required=True)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--spreadsheet-id")
    p.add_argument("--publish", action="store_true", help="append to authorized new-Looker ledger; default offline")
    args = p.parse_args()
    try:
        result = run(args.reports_root, args.state_dir, args.publish, args.spreadsheet_id)
    except BlockingIOError:
        print('{"status":"already_running"}')
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
