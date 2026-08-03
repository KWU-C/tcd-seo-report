"""GA4 activeUsers取得（「②サイトアクセス状況」セクション用）。

責務は「セグメント（サイト全体/マガジン/Works/Service到達）ごとのactiveUsersを
取得して返す」ことのみ。GSC/AIOと異なり判断材料の主役ではなく参考情報のため、
取得に失敗してもレポート生成全体は止めない（Fail Fastの対象外。セグメント単位で
errorを記録し、他セグメントの取得は継続する）。

セグメントは「到達ユーザー」（期間中に一度でも該当パスへ到達したユーザー数）。
複数セグメントにまたがってカウントされるため、合計してもサイト全体の値と一致しない
（config_v2/pipeline.yaml のコメント・ユーザー確認済み仕様）。

使用指標は activeUsers のみ。active28DayUsers はページディメンションと組み合わせると
重複集計になり誤解を招くため使用しない。
"""
from __future__ import annotations

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Filter,
    FilterExpression,
    Metric,
    RunReportRequest,
)

from src_v2.auth import get_ga4_credentials


def _build_client(credentials_file: str = "credentials.json") -> BetaAnalyticsDataClient:
    creds = get_ga4_credentials(credentials_file)
    return BetaAnalyticsDataClient(credentials=creds)


def _fetch_active_users(
    client: BetaAnalyticsDataClient,
    property_id: str,
    start_date: str,
    end_date: str,
    path_contains: str | None,
) -> int:
    dimension_filter = None
    if path_contains:
        dimension_filter = FilterExpression(
            filter=Filter(
                field_name="pagePathPlusQueryString",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.CONTAINS,
                    value=path_contains,
                ),
            )
        )
    request = RunReportRequest(
        property=f"properties/{property_id}",
        metrics=[Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=dimension_filter,
    )
    response = client.run_report(request)
    if not response.rows:
        return 0
    return int(response.rows[0].metric_values[0].value)


def fetch_segments(
    ga4_cfg: dict,
    cur_start: str,
    cur_end: str,
    prv_start: str,
    prv_end: str,
    credentials_file: str = "credentials.json",
) -> dict[str, dict]:
    """config_v2/pipeline.yaml の ga4.segments 各項目について当期/前期のactiveUsersを取得する。

    戻り値: {segment_key: {"label", "current", "comparison", "error"}}
    取得失敗時は current/comparison が None になり、error にメッセージが入る
    （このセグメントのみ失敗として扱い、他は継続する）。
    """
    property_id = ga4_cfg["property_id"]

    try:
        client = _build_client(credentials_file)
    except Exception as e:  # noqa: BLE001 - GA4取得は非致命的にerror記録のみで継続する
        return {
            seg["key"]: {"label": seg["label"], "current": None, "comparison": None, "error": str(e)}
            for seg in ga4_cfg["segments"]
        }

    result: dict[str, dict] = {}
    for seg in ga4_cfg["segments"]:
        key = seg["key"]
        try:
            current = _fetch_active_users(client, property_id, cur_start, cur_end, seg.get("path_contains"))
            comparison = _fetch_active_users(client, property_id, prv_start, prv_end, seg.get("path_contains"))
            result[key] = {"label": seg["label"], "current": current, "comparison": comparison, "error": None}
        except Exception as e:  # noqa: BLE001 - セグメント単位で失敗を隔離する
            result[key] = {"label": seg["label"], "current": None, "comparison": None, "error": str(e)}

    return result
