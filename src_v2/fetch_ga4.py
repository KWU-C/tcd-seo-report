"""GA4 の取得（① サイト全体のアクセス状況）。

取得するもの（指標名を混同しないよう、データにも指標名を持たせる）:
  - セグメント別の到達ユーザー（activeUsers）: 期間中に条件に合うページを1回以上表示したユーザー数。
    当週・前週の合計と、日別の値（異常値の検出用）。
  - 流入元別のセッション（sessions・engagedSessions）: sessionSourceMedium ごとに取得し、
    広告／自然検索／直接／参照元／生成AI／その他・不明に分類する（src_v2/ga4_channels.py）。
  - 推移（直近 N 週、月〜日）: アクティブユーザー数（全体・広告を除く）とセッション数（全体・広告・広告を除く）。

GSC/AIO と異なり判断材料の主役ではなく参考情報のため、取得に失敗してもレポート生成全体は止めない
（Fail Fast の対象外。項目ごとに error を記録し、他の取得は継続する）。

ページの条件は pagePath（クエリ文字列を含まない）の完全一致（exact）・前方一致（prefix）で指定する。
旧方式の部分一致（path_contains、2026-11-16週まで）は過去データの再現用にだけ残す。
active28DayUsers はページディメンションと組み合わせると重複集計になり誤解を招くため使用しない。
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    RunReportRequest,
)

from src_v2.auth import get_ga4_credentials
from src_v2.ga4_channels import AD, CHANNEL_KEYS, classify_channel

ACCESS_VERSION = 2
METRIC_USERS = "activeUsers"
METRIC_SESSIONS = "sessions"


def _build_client(credentials_file: str = "credentials.json") -> BetaAnalyticsDataClient:
    creds = get_ga4_credentials(credentials_file)
    return BetaAnalyticsDataClient(credentials=creds)


def _hostname_filter(hostnames: list[str] | None) -> FilterExpression | None:
    """本番ホスト名だけに絞る条件（config_v2/pipeline.yaml の ga4.hostnames）。空・未設定なら絞らない。"""
    if not hostnames:
        return None
    return FilterExpression(filter=Filter(
        field_name="hostName", in_list_filter=Filter.InListFilter(values=list(hostnames))))


def _and_filters(*exprs: FilterExpression | None) -> FilterExpression | None:
    exprs = [e for e in exprs if e is not None]
    if not exprs:
        return None
    if len(exprs) == 1:
        return exprs[0]
    return FilterExpression(and_group=FilterExpressionList(expressions=exprs))


def segment_filter(seg: dict) -> FilterExpression | None:
    """セグメントのページ条件。exact（完全一致）と prefix（前方一致）の OR。どちらも無ければ全ページ。

    旧方式の path_contains（pagePathPlusQueryString の部分一致）は、過去の設定を読むためだけに残す。
    """
    exprs = []
    if seg.get("exact"):
        exprs.append(FilterExpression(filter=Filter(
            field_name="pagePath", in_list_filter=Filter.InListFilter(values=list(seg["exact"])))))
    for prefix in seg.get("prefix") or []:
        exprs.append(FilterExpression(filter=Filter(
            field_name="pagePath", string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.BEGINS_WITH, value=prefix))))
    if not exprs and seg.get("path_contains"):
        exprs.append(FilterExpression(filter=Filter(
            field_name="pagePathPlusQueryString", string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS, value=seg["path_contains"]))))
    if not exprs:
        return None
    if len(exprs) == 1:
        return exprs[0]
    return FilterExpression(or_group=FilterExpressionList(expressions=exprs))


def segment_rule(seg: dict) -> dict:
    """集計条件の記録（前週の速報値と条件が同じかを確かめるため、normalized.json に残す）。"""
    return {"exact": list(seg.get("exact") or []), "prefix": list(seg.get("prefix") or []),
            "path_contains": seg.get("path_contains")}


def _non_ad_filter(channels_cfg: dict) -> FilterExpression:
    """広告以外のセッション（sessionMedium が広告の正規表現に一致しない）。"""
    return FilterExpression(not_expression=FilterExpression(filter=Filter(
        field_name="sessionMedium", string_filter=Filter.StringFilter(
            match_type=Filter.StringFilter.MatchType.FULL_REGEXP,
            value=channels_cfg["ad_medium_regex"], case_sensitive=False))))


def _run(client: BetaAnalyticsDataClient, property_id: str, dims: list[str], metrics: list[str],
         ranges: list[tuple[str, str, str]], dimension_filter: FilterExpression | None):
    """ranges: [(name, start, end)]。期間が2つ以上のとき、GA4 は dateRange ディメンションを末尾に加える。"""
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in metrics],
        date_ranges=[DateRange(start_date=s, end_date=e, name=n) for n, s, e in ranges],
        dimension_filter=dimension_filter,
        limit=100000,
    )
    response = client.run_report(request)
    if response.row_count > len(response.rows):
        raise RuntimeError(f"GA4の行数上限を超えた（{response.row_count}行中{len(response.rows)}行）")
    names = [h.name for h in response.dimension_headers]
    out = []
    for row in response.rows:
        keys = {names[i]: v.value for i, v in enumerate(row.dimension_values)}
        values = [float(v.value) for v in row.metric_values]
        out.append((keys, values))
    return out


def _ymd(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


# ── セグメント別の到達ユーザー ─────────────────────────────────────

def _fetch_segment(client, property_id: str, seg: dict, hostnames, cur: tuple[str, str], prv: tuple[str, str]) -> dict:
    flt = _and_filters(_hostname_filter(hostnames), segment_filter(seg))
    totals = {"current": 0, "comparison": 0}
    rows = _run(client, property_id, [], [METRIC_USERS],
                [("current", *cur), ("comparison", *prv)], flt)
    for keys, values in rows:
        totals[keys.get("dateRange", "current")] = int(values[0])
    daily = {}
    for keys, values in _run(client, property_id, ["date"], [METRIC_USERS], [("all", prv[0], cur[1])], flt):
        daily[_ymd(keys["date"])] = int(values[0])
    return {"current": totals["current"], "comparison": totals["comparison"], "daily": dict(sorted(daily.items()))}


def fetch_segments(
    ga4_cfg: dict,
    cur_start: str,
    cur_end: str,
    prv_start: str,
    prv_end: str,
    credentials_file: str = "credentials.json",
    client: BetaAnalyticsDataClient | None = None,
) -> dict[str, dict]:
    """ga4.segments の各項目について、当週・前週の到達ユーザー（activeUsers）と日別の値を取得する。

    戻り値: {segment_key: {"label", "rule_label", "group", "metric", "rule", "current", "comparison", "daily", "error"}}
    取得失敗時は current/comparison が None になり、error にメッセージが入る（このセグメントのみ失敗として扱う）。
    """
    property_id = ga4_cfg["property_id"]
    hostnames = ga4_cfg.get("hostnames")

    def base(seg):
        return {"label": seg["label"], "rule_label": seg.get("rule_label"), "group": seg.get("group", "main"),
                "metric": METRIC_USERS, "rule": segment_rule(seg)}

    try:
        client = client or _build_client(credentials_file)
    except Exception as e:  # noqa: BLE001 - GA4取得は非致命的にerror記録のみで継続する
        return {seg["key"]: {**base(seg), "current": None, "comparison": None, "daily": {}, "error": str(e)}
                for seg in ga4_cfg["segments"]}

    result: dict[str, dict] = {}
    for seg in ga4_cfg["segments"]:
        try:
            values = _fetch_segment(client, property_id, seg, hostnames, (cur_start, cur_end), (prv_start, prv_end))
            result[seg["key"]] = {**base(seg), **values, "error": None}
        except Exception as e:  # noqa: BLE001 - セグメント単位で失敗を隔離する
            result[seg["key"]] = {**base(seg), "current": None, "comparison": None, "daily": {}, "error": str(e)}
    return result


# ── 流入元別のセッション ─────────────────────────────────────────

def build_channel_rows(raw: dict[str, dict[str, dict]], channels_cfg: dict) -> list[dict]:
    """{period: {source_medium: {"sessions", "engaged_sessions"}}} を参照元ごとの行（区分つき）にする。"""
    rows = []
    for sm in sorted(set(raw.get("current", {})) | set(raw.get("comparison", {}))):
        channel, service = classify_channel(sm, channels_cfg)
        cur = raw.get("current", {}).get(sm, {"sessions": 0, "engaged_sessions": 0})
        prv = raw.get("comparison", {}).get(sm, {"sessions": 0, "engaged_sessions": 0})
        if not (cur["sessions"] or prv["sessions"]):
            continue
        rows.append({"source_medium": sm, "channel": channel, "ai_service": service, "current": cur, "comparison": prv})
    return rows


def fetch_channels(ga4_cfg: dict, cur: tuple[str, str], prv: tuple[str, str], client) -> dict:
    """流入元（sessionSourceMedium）別のセッション数・エンゲージメントのあったセッション数（当週・前週）。"""
    try:
        rows = _run(client, ga4_cfg["property_id"], ["sessionSourceMedium"], ["sessions", "engagedSessions"],
                    [("current", *cur), ("comparison", *prv)], _hostname_filter(ga4_cfg.get("hostnames")))
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(e).__name__}: {e}", "metric": METRIC_SESSIONS, "rows": []}
    raw: dict[str, dict[str, dict]] = {"current": {}, "comparison": {}}
    for keys, values in rows:
        raw[keys["dateRange"]][keys["sessionSourceMedium"]] = {"sessions": int(values[0]), "engaged_sessions": int(values[1])}
    return {"status": "ok", "error": None, "metric": METRIC_SESSIONS,
            "rows": build_channel_rows(raw, ga4_cfg["channels"])}


# ── 推移（直近 N 週） ───────────────────────────────────────────

def _iso_week_start(value: str) -> str:
    """GA4 の isoYearIsoWeek（例: 202646）→ その週の月曜日。"""
    return date.fromisocalendar(int(value[:4]), int(value[4:]), 1).isoformat()


def trend_range(report_week_start: str, weeks: int) -> tuple[str, str]:
    start = date.fromisoformat(report_week_start) - timedelta(days=7 * (weeks - 1))
    return start.isoformat(), (date.fromisoformat(report_week_start) + timedelta(days=6)).isoformat()


def fetch_trend(ga4_cfg: dict, report_week_start: str, client) -> dict:
    """直近 N 週（月〜日）のアクティブユーザー数（全体・広告を除く）とセッション数（区分別）。"""
    weeks = int(ga4_cfg.get("trend_weeks", 8))
    start, end = trend_range(report_week_start, weeks)
    channels_cfg = ga4_cfg["channels"]
    host = _hostname_filter(ga4_cfg.get("hostnames"))
    pid = ga4_cfg["property_id"]
    try:
        by_week: dict[str, dict] = {}

        def week(w):
            return by_week.setdefault(_iso_week_start(w), {
                "users_total": 0, "users_non_ad": 0, "sessions_total": 0,
                "sessions_by_channel": {k: 0 for k in CHANNEL_KEYS}})

        for keys, values in _run(client, pid, ["isoYearIsoWeek"], [METRIC_USERS], [("all", start, end)], host):
            week(keys["isoYearIsoWeek"])["users_total"] = int(values[0])
        for keys, values in _run(client, pid, ["isoYearIsoWeek"], [METRIC_USERS], [("all", start, end)],
                                 _and_filters(host, _non_ad_filter(channels_cfg))):
            week(keys["isoYearIsoWeek"])["users_non_ad"] = int(values[0])
        for keys, values in _run(client, pid, ["isoYearIsoWeek", "sessionSourceMedium"], [METRIC_SESSIONS],
                                 [("all", start, end)], host):
            w = week(keys["isoYearIsoWeek"])
            channel, _ = classify_channel(keys["sessionSourceMedium"], channels_cfg)
            w["sessions_by_channel"][channel] += int(values[0])
            w["sessions_total"] += int(values[0])
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "error": f"{type(e).__name__}: {e}", "start": start, "end": end, "weeks": []}

    rows = []
    for k in range(weeks):
        ws = (date.fromisoformat(start) + timedelta(days=7 * k)).isoformat()
        w = by_week.get(ws) or {"users_total": 0, "users_non_ad": 0, "sessions_total": 0,
                                "sessions_by_channel": {c: 0 for c in CHANNEL_KEYS}}
        ad = w["sessions_by_channel"][AD]
        rows.append({"week_start": ws, **w, "sessions_ad": ad, "sessions_non_ad": w["sessions_total"] - ad})
    return {"status": "ok", "error": None, "start": start, "end": end, "weeks": rows}


def fetch_access(ga4_cfg: dict, period: dict, credentials_file: str = "credentials.json",
                 client: BetaAnalyticsDataClient | None = None) -> tuple[dict, dict]:
    """① の GA4 一式。戻り値: (segments, {"version", "channels", "trend"})。失敗は項目ごとに記録して継続する。"""
    cur = (period["report_week_start"], period["report_week_end"])
    prv = (period["ga4_prv_start"], period["ga4_prv_end"])
    try:
        client = client or _build_client(credentials_file)
    except Exception as e:  # noqa: BLE001
        segments = fetch_segments(ga4_cfg, *cur, *prv, credentials_file=credentials_file)
        err = f"{type(e).__name__}: {e}"
        return segments, {"version": ACCESS_VERSION,
                          "channels": {"status": "failed", "error": err, "metric": METRIC_SESSIONS, "rows": []},
                          "trend": {"status": "failed", "error": err, "weeks": []}}
    segments = fetch_segments(ga4_cfg, *cur, *prv, client=client)
    return segments, {"version": ACCESS_VERSION,
                      "channels": fetch_channels(ga4_cfg, cur, prv, client),
                      "trend": fetch_trend(ga4_cfg, period["report_week_start"], client)}


def backfill_access(period_dir: Path, ga4_cfg: dict, credentials_file: str = "credentials.json",
                    force: bool = False) -> bool:
    """取得済みの normalized.json に新しい①（ACCESS_VERSION）が無い場合だけ、GA4 を取り直して書き込む。

    確認用（dry-run）で取得済みデータを使い回すとき（GSC・AIO を取り直さない）に使う。書き込んだら True。
    """
    path = Path(period_dir) / "normalized.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    period = bundle["period"]
    if "ga4_prv_start" not in period:
        return False  # 旧方式（〜2026-10-30）の週は対象外
    if (bundle.get("ga4_access") or {}).get("version") == ACCESS_VERSION and not force:
        return False
    segments, access = fetch_access(ga4_cfg, period, credentials_file)
    bundle["ga4"] = segments
    bundle["ga4_access"] = access
    period["ga4_hostnames"] = list(ga4_cfg.get("hostnames") or [])
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def main() -> None:
    from src_v2 import config_loader

    parser = argparse.ArgumentParser(description="取得済みの週に、GA4（①）の新しい集計を書き込む")
    parser.add_argument("--period-dir", required=True, help="normalized.json のあるフォルダ")
    parser.add_argument("--credentials-file", default="credentials.json")
    parser.add_argument("--force", action="store_true", help="書き込み済みでも取り直す")
    args = parser.parse_args()
    ga4_cfg = config_loader.load_pipeline_config()["ga4"]
    if backfill_access(Path(args.period_dir), ga4_cfg, args.credentials_file, args.force):
        print("GA4: 新しい集計（セグメント・流入元・推移）を書き込んだ")
    else:
        print("GA4: 書き込み済み（または旧方式の週）のため取り直さない")


if __name__ == "__main__":
    main()
