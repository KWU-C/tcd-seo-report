"""AIO（Google AI Overview）観測データ取得（raw保存まで）。

責務は「SerpAPIを呼び、結果をそのままdata_v2/raw/aio/に保存する」ことのみ。
引用ドメインの抽出・alias正規化・TCD引用判定などの解釈は一切ここでは行わない
（normalize.py の責務。raw層に解釈済みの値を混ぜない）。

acquisition_status の判定だけは取得層の責務とする
（「未取得」と「観測したが引用なし」を区別するために、取得結果からしか
分からない情報のため）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src_v2.schema import (
    ACQUISITION_STATUS_FAILED,
    ACQUISITION_STATUS_NOT_GENERATED,
    ACQUISITION_STATUS_NOT_RUN,
    ACQUISITION_STATUS_SUCCESS,
)

RAW_DIR = Path("data_v2/raw/aio")
PROVIDER = "serpapi"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save(record: dict, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts_slug = record["observed_at"].replace(":", "").replace("-", "")
    query_slug = "".join(ch if ch.isalnum() else "_" for ch in record["query"])[:40]
    path = raw_dir / f"{ts_slug}_{query_slug}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _not_run_record(query: str, reason: str) -> dict:
    return {
        "observed_at": _now_iso(),
        "provider": PROVIDER,
        "query": query,
        "acquisition_status": ACQUISITION_STATUS_NOT_RUN,
        "raw_response": None,
        "error_message": reason,
    }


def fetch_priority_queries(
    queries: list[str],
    serp_params: dict | None = None,
    raw_dir: Path | str = RAW_DIR,
) -> list[dict]:
    """重点クエリ群についてAIO観測を行い、data-contract.md 2.2のスキーマでraw保存する。
    戻り値はraw record（保存済みファイルと同一内容）のリスト。

    APIキー未設定は「失敗」ではなく「not_run」として扱う
    （requirements.md 6.2: not_runを引用なしと混同しない）。
    """
    raw_dir = Path(raw_dir)
    api_key = os.environ.get("SERPAPI_API_KEY")

    if not api_key:
        print("  SERPAPI_API_KEY が未設定のため AIO 観測を not_run として記録します。", file=sys.stderr)
        records = [_not_run_record(q, "SERPAPI_API_KEY 未設定") for q in queries]
        for r in records:
            _save(r, raw_dir)
        return records

    try:
        from serpapi import GoogleSearch
    except ImportError:
        print("  google-search-results 未インストールのため AIO 観測を not_run として記録します。", file=sys.stderr)
        records = [_not_run_record(q, "google-search-results 未インストール") for q in queries]
        for r in records:
            _save(r, raw_dir)
        return records

    cfg = serp_params or {}
    records: list[dict] = []

    for query in queries:
        observed_at = _now_iso()
        params = {
            "engine": cfg.get("engine", "google"),
            "q": query,
            "google_domain": cfg.get("google_domain", "google.co.jp"),
            "hl": cfg.get("hl", "ja"),
            "gl": cfg.get("gl", "jp"),
            "location": cfg.get("location", "Japan"),
            "api_key": api_key,
        }

        status: str
        raw_response: dict | None
        error_message: str | None

        try:
            search_result = GoogleSearch(params).get_dict()
            ai_overview = search_result.get("ai_overview")
            ai_overview_result: dict | None = None

            # SerpAPIはAIO本文の代わりに ai_overview: {page_token, serpapi_link} だけを返すことがある
            # （本文は別リクエストで取得する仕様。page_tokenは短時間で失効するため直後に取得する）。
            # この場合に追加取得しないと、引用元が空のまま「AIO生成あり」と記録されてしまう。
            has_content = bool(ai_overview and (ai_overview.get("references") or ai_overview.get("text_blocks")))
            page_token = (ai_overview or {}).get("page_token") or search_result.get("ai_overview_page_token")
            if not has_content and page_token:
                ai_overview_result = GoogleSearch({
                    "engine": "google_ai_overview",
                    "page_token": page_token,
                    "api_key": api_key,
                }).get_dict()
                ai_overview = ai_overview_result.get("ai_overview") or ai_overview

            status = ACQUISITION_STATUS_SUCCESS if ai_overview else ACQUISITION_STATUS_NOT_GENERATED
            raw_response = {"search_result": search_result, "ai_overview_result": ai_overview_result}
            error_message = None
        except Exception as e:  # noqa: BLE001 - SerpAPI例外の型が非公開のため広く捕捉し、failedとして記録する
            status = ACQUISITION_STATUS_FAILED
            raw_response = None
            error_message = str(e)
            print(f"  SerpAPI エラー ({query}): {e}", file=sys.stderr)

        record = {
            "observed_at": observed_at,
            "provider": PROVIDER,
            "query": query,
            "acquisition_status": status,
            "raw_response": raw_response,
            "error_message": error_message,
        }
        _save(record, raw_dir)
        records.append(record)

    return records
