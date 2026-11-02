#!/usr/bin/env python3
"""問い合わせ実績（Dot の週次集計値）を Google スプレッドシートから読み取る（2026-11-02〜）。

責務は「スプレッドシートの各行を読み、行ごとに検査して状態を付ける」ことのみ。
どの週の値をレポートに使うか（手入力の inquiries.yaml との優先順位・累計）は view_model.py が決める。

取得元: TCD Workspace のスプレッドシート「reports_v2 週次問い合わせ集計」、タブ weekly。
1行目は見出し（列名で読むため、列の順番は問わない）。1週1行。件数だけを記録し、個人情報は持たない。

問い合わせ実績は判断材料の補助のため、取得に失敗してもレポート生成は止めない（Fail Fast の対象外）。
失敗・未設定は snapshot["status"] に記録し、レポートでは「未集計」と表示する（0件として扱わない）。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src_v2.config_loader import INQUIRY_COUNT_KEYS

JST = timezone(timedelta(hours=9))

COLUMNS = (
    "week_start", "week_end", "status",
    *INQUIRY_COUNT_KEYS,
    "excluded_replies", "confirmed_by", "confirmed_at", "note",
)
STATUS_CONFIRMED = "確定"
STATUS_IN_PROGRESS = "集計中"

# 行の状態（parse_rows の戻り値の "state"）
ROW_CONFIRMED = "confirmed"
ROW_IN_PROGRESS = "in_progress"
ROW_INVALID = "invalid"

# スナップショットの状態
SNAPSHOT_OK = "ok"
SNAPSHOT_NOT_CONFIGURED = "not_configured"
SNAPSHOT_FETCH_FAILED = "fetch_failed"

_BREAKDOWN_KEYS = ("project_consultation", "sales_solicitation", "other")


def _to_int(value: Any) -> int | None:
    """スプレッドシートの数値（UNFORMATTED_VALUE では 10 や 10.0）を整数にする。整数でなければ None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _row_to_dict(header: list[str], row: list[Any]) -> dict:
    return {name: (row[i] if i < len(row) else "") for i, name in enumerate(header)}


def parse_row(raw: dict) -> dict:
    """1行を検査し、{"week_start", "week_end", "state", "errors", counts..., meta...} を返す。"""
    errors: list[str] = []
    start = _to_date(raw.get("week_start"))
    end = _to_date(raw.get("week_end"))
    if start is None:
        errors.append(f"week_start の日付が不正: {raw.get('week_start')!r}")
    elif start.weekday() != 0:
        errors.append(f"week_start が月曜日ではない: {start}")
    if start is not None and end != start + timedelta(days=6):
        errors.append(f"week_end が week_start の6日後（日曜）ではない: {raw.get('week_end')!r}")

    status = str(raw.get("status", "")).strip()
    parsed: dict[str, Any] = {
        "week_start": start.isoformat() if start else str(raw.get("week_start", "")),
        "week_end": end.isoformat() if end else str(raw.get("week_end", "")),
        "status": status,
        "confirmed_by": str(raw.get("confirmed_by", "")).strip() or None,
        "confirmed_at": str(raw.get("confirmed_at", "")).strip() or None,
    }

    if status == STATUS_IN_PROGRESS:
        return {**parsed, "state": ROW_INVALID if errors else ROW_IN_PROGRESS, "errors": errors}
    if status != STATUS_CONFIRMED:
        errors.append(f"status は「{STATUS_CONFIRMED}」か「{STATUS_IN_PROGRESS}」: {status!r}")

    counts: dict[str, int] = {}
    for key in INQUIRY_COUNT_KEYS:
        value = _to_int(raw.get(key))
        if value is None or value < 0:
            errors.append(f"{key} は0以上の整数: {raw.get(key)!r}")
        else:
            counts[key] = value
    if len(counts) == len(INQUIRY_COUNT_KEYS):
        breakdown = sum(counts[k] for k in _BREAKDOWN_KEYS)
        if breakdown != counts["general_total"]:
            errors.append(f"内訳合計 {breakdown}（案件相談+売り込み・提携勧誘+その他）が一般問い合わせ {counts['general_total']} と一致しない")
        if counts["other_needs_review"] > counts["other"]:
            errors.append("other_needs_review が other を超えている")

    state = ROW_INVALID if errors else ROW_CONFIRMED
    return {**parsed, **counts, "state": state, "errors": errors}


def parse_rows(values: list[list[Any]]) -> list[dict]:
    """シートの値（1行目が見出し）を検査済みの行のリストにする。空行は無視する。
    同じ週の行が複数ある場合は、どれを使うか決められないため、その週の行をすべて不整合にする。"""
    if not values:
        return []
    header = [str(h).strip() for h in values[0]]
    missing = [c for c in ("week_start", "week_end", "status", *INQUIRY_COUNT_KEYS) if c not in header]
    if missing:
        raise ValueError(f"見出し行に必要な列が無い: {', '.join(missing)}")

    rows = []
    for raw_row in values[1:]:
        if not any(str(v).strip() for v in raw_row):
            continue
        rows.append(parse_row(_row_to_dict(header, raw_row)))

    dup = {w for w, n in Counter(r["week_start"] for r in rows).items() if n > 1}
    for r in rows:
        if r["week_start"] in dup:
            r["state"] = ROW_INVALID
            r["errors"] = r["errors"] + ["同じ週の行が複数ある"]
    return rows


def _now_iso() -> str:
    return datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def fetch_values(spreadsheet_id: str, value_range: str, credentials_file: str = "credentials.json") -> list[list[Any]]:
    from googleapiclient.discovery import build

    from src_v2.auth import get_sheets_credentials

    creds = get_sheets_credentials(credentials_file)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=value_range,
        valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="FORMATTED_STRING",
    ).execute()
    return resp.get("values", [])


def fetch_snapshot(inquiries_cfg: dict | None, credentials_file: str = "credentials.json",
                   fetcher=fetch_values) -> dict:
    """スプレッドシートの全行を取得・検査したスナップショット。例外は送出しない（status に記録する）。"""
    snapshot: dict[str, Any] = {"source": "sheets", "fetched_at": _now_iso(), "status": SNAPSHOT_OK,
                                "error": None, "rows": []}
    cfg = inquiries_cfg or {}
    sheet_id = os.environ.get(cfg.get("sheet_id_env", "INQUIRY_SHEET_ID"), "").strip()
    if cfg.get("source") != "sheets" or not sheet_id:
        snapshot.update(status=SNAPSHOT_NOT_CONFIGURED,
                        error="スプレッドシートが未設定（.env の INQUIRY_SHEET_ID）")
        return snapshot
    try:
        values = fetcher(sheet_id, cfg.get("range", "weekly!A1:M"), credentials_file)
        snapshot["rows"] = parse_rows(values)
    except Exception as e:  # noqa: BLE001 - 問い合わせ実績の取得失敗ではレポート生成を止めない
        snapshot.update(status=SNAPSHOT_FETCH_FAILED, error=f"{type(e).__name__}: {e}")
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="問い合わせ集計スプレッドシートの認可・取得確認")
    parser.add_argument("--authorize", action="store_true", help="ブラウザで読み取り権限を認可する（初回のみ）")
    parser.add_argument("--credentials-file", default="credentials.json")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    from src_v2 import config_loader
    from src_v2.auth import get_sheets_credentials

    if args.authorize:
        get_sheets_credentials(args.credentials_file, interactive=True)
        print("認可しました（token_sheets.pickle）")

    snapshot = fetch_snapshot(config_loader.load_pipeline_config().get("inquiries"), args.credentials_file)
    print(f"取得状態: {snapshot['status']}" + (f"（{snapshot['error']}）" if snapshot["error"] else ""))
    for r in snapshot["rows"]:
        detail = "／".join(r["errors"]) if r["errors"] else ""
        print(f"  {r['week_start']}〜{r['week_end']} {r['state']} {detail}")
    if snapshot["status"] == SNAPSHOT_FETCH_FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
