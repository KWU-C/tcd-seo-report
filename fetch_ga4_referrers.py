#!/usr/bin/env python3
"""
GA4 サイト内流入元取得スクリプト
お問い合わせ・資料ダウンロードページへの流入元上位3ページを取得します

使い方:
  python3 fetch_ga4_referrers.py
"""

import pickle
from pathlib import Path
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    Dimension,
    Metric,
    DateRange,
    FilterExpression,
    Filter,
)

PROPERTY_ID = "269009748"
GA4_TOKEN_FILE = "token_ga4.pickle"
CREDENTIALS_FILE = "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

# レポート対象期間
START_DATE = "2026-05-26"
END_DATE = "2026-06-01"

# 対象ページ
TARGET_PAGES = {
    "お問い合わせ": "/contact",
    "資料ダウンロード": "/download",
}


def get_credentials():
    creds = None
    token_path = Path(GA4_TOKEN_FILE)
    if token_path.exists():
        with open(token_path, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)
    return creds


def fetch_internal_referrers(client, page_path: str, page_label: str):
    """指定ページへのサイト内流入元上位3件を取得"""
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="pageReferrer")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date=START_DATE, end_date=END_DATE)],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="pagePath",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value=page_path,
                ),
            )
        ),
        limit=10,
        order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
    )
    response = client.run_report(request)

    EXCLUDE_PATTERNS = [
        "confirm", "thanks", "complete", "finish",
        "contact_confirm", "contact_thanks",
        "download_complete", "download_thanks",
        "確認", "完了", "送信済",
    ]

    print(f"\n=== {page_label} ({page_path}) ===")
    internal = []
    for row in response.rows:
        referrer = row.dimension_values[0].value
        sessions = int(row.metric_values[0].value)
        if "tcd.jp" not in referrer:
            continue
        if referrer == f"https://tcd.jp{page_path}":
            continue
        if any(p in referrer.lower() for p in EXCLUDE_PATTERNS):
            continue
        internal.append((referrer, sessions))

    if not internal:
        print("  サイト内流入元なし（または直接流入のみ）")
    else:
        for i, (url, count) in enumerate(internal[:3], 1):
            # URLからページ名を簡易抽出
            path = url.replace("https://tcd.jp", "").split("?")[0]
            print(f"  {i}. {path}  ({count} sessions)")


def main():
    print("GA4 認証中...")
    creds = get_credentials()

    client = BetaAnalyticsDataClient(credentials=creds)
    print(f"接続完了 — Property: {PROPERTY_ID}  期間: {START_DATE} 〜 {END_DATE}")

    for label, path in TARGET_PAGES.items():
        fetch_internal_referrers(client, path, label)

    print("\n取得完了。上記の結果をレポートに転記してください。")


if __name__ == "__main__":
    main()
