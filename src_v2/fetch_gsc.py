"""Search Console データ取得（raw保存まで）。

責務は「APIから取得してdata_v2/raw/gsc/にそのまま保存する」ことのみ。
正規化・検証はここで行わない（normalize.py / validate.py の責務）。

Fail Fast: APIエラーを握りつぶさない。取得に失敗したら GscFetchError を送出し、
呼び出し側（pipeline.py）でレポート生成に進まないようにする。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from src_v2.schema import GscFetchError

SC_MAX_ROWS = 25_000
RAW_DIR = Path("data_v2/raw/gsc")

# data-contract.md 2.1 が定義する4種類の粒度
DIMENSION_SETS: dict[str, list[str]] = {
    "date": ["date"],
    "date_query": ["date", "query"],
    "date_page": ["date", "page"],
    "date_page_query": ["date", "page", "query"],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_rows_paginated(
    service: Any,
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    row_limit: int,
) -> list[dict]:
    """ページネーション込みで取得する。APIエラーは握りつぶさずGscFetchErrorにする。"""
    rows: list[dict] = []
    start_row = 0

    while len(rows) < row_limit:
        body: dict = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions,
            "rowLimit": min(SC_MAX_ROWS, row_limit - len(rows)),
            "startRow": start_row,
        }
        try:
            resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        except HttpError as e:
            raise GscFetchError(
                f"Search Console API取得失敗: dimensions={dimensions} "
                f"period={start_date}..{end_date} error={e}"
            ) from e

        batch = resp.get("rows", [])
        if not batch:
            break

        rows.extend(batch)
        start_row += len(batch)
        if len(batch) < SC_MAX_ROWS:
            break

    return rows


def fetch_dimension(
    service: Any,
    site_url: str,
    dimension_key: str,
    start_date: str,
    end_date: str,
    row_limit: int = SC_MAX_ROWS,
    raw_dir: Path | str = RAW_DIR,
) -> tuple[Path, dict]:
    """1つの粒度（date/date_query/date_page/date_page_query）を取得し、
    data-contract.md 2.1 のスキーマでraw保存する。戻り値は (保存先パス, raw dict)。
    """
    if dimension_key not in DIMENSION_SETS:
        raise ValueError(f"未知のdimension_key: {dimension_key}")
    dimensions = DIMENSION_SETS[dimension_key]

    fetched_at = _now_iso()
    rows = _fetch_rows_paginated(service, site_url, start_date, end_date, dimensions, row_limit)

    raw = {
        "fetched_at": fetched_at,
        "site_url": site_url,
        "dimensions": dimensions,
        "date_start": start_date,
        "date_end": end_date,
        "row_limit": row_limit,
        "response": {"rows": rows},
    }

    out_dir = Path(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at_slug = fetched_at.replace(":", "").replace("-", "")
    out_path = out_dir / f"{fetched_at_slug}_{dimension_key}_{start_date}_{end_date}.json"
    out_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    return out_path, raw


def fetch_period(
    service: Any,
    site_url: str,
    start_date: str,
    end_date: str,
    row_limit: int = SC_MAX_ROWS,
    raw_dir: Path | str = RAW_DIR,
) -> dict[str, dict]:
    """1期間分（date/date_query/date_page/date_page_query）をまとめて取得する。
    戻り値: {dimension_key: raw dict} 。いずれか1つでも失敗すればGscFetchErrorが伝播する。
    """
    result: dict[str, dict] = {}
    for dimension_key in DIMENSION_SETS:
        _, raw = fetch_dimension(
            service, site_url, dimension_key, start_date, end_date,
            row_limit=row_limit, raw_dir=raw_dir,
        )
        result[dimension_key] = raw
    return result
