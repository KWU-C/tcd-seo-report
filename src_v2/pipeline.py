#!/usr/bin/env python3
"""Phase 2 パイプライン: raw取得 → 検証 → 正規化。

責務を分離する:
  fetch_gsc / fetch_aio  … rawをdata_v2/raw/に保存するだけ
  validate                … rawを検証し、data_quality_reportを作る（Fail Fast）
  normalize                … 検証済みrawをprocessedテーブルへ変換する

レポート生成（analyze / render）はPhase 4以降の別責務であり、本スクリプトは
正規化済みデータの出力までを行う。

データ取得に失敗した場合はレポート（processed出力）を生成しない。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src_v2 import config_loader, fetch_aio, fetch_ga4, fetch_gsc, fetch_inquiries, normalize, validate
from googleapiclient.errors import HttpError

from src_v2.auth import get_search_console_service
from src_v2.schema import SCHEMA_VERSION, DataQualityError, GscFetchError

PROCESSED_DIR = Path("data_v2/processed")
SITE_URL = "https://tcd.jp/"


JST = timezone(timedelta(hours=9))


def get_weekly_period(today: datetime | None = None, pipeline_cfg: dict | None = None) -> dict:
    """旧方式（gsc.data_cutoff.mode=fixed）: 実行日からoffset_days日前を末日とする7日間（GSC・GA4共通）。

    2026-11-02以前のレポートの期間決定。mode=auto（現行）では使わないが、
    過去の期間を再現できるように残す。
    """
    today = today or datetime.today()
    pipeline_cfg = pipeline_cfg or config_loader.load_pipeline_config()
    fmt = lambda d: d.strftime("%Y-%m-%d")

    cutoff_cfg = pipeline_cfg["gsc"]["data_cutoff"]
    length_days = pipeline_cfg["gsc"]["report_period"]["length_days"]

    cur_end = today - timedelta(days=cutoff_cfg["offset_days"])
    cur_start = cur_end - timedelta(days=length_days - 1)
    prv_end = cur_start - timedelta(days=1)
    prv_start = prv_end - timedelta(days=length_days - 1)

    return {
        "cur_start": fmt(cur_start), "cur_end": fmt(cur_end),
        "prv_start": fmt(prv_start), "prv_end": fmt(prv_end),
    }


def get_report_week(today: date | None = None, week_start: date | None = None) -> dict:
    """レポート週（GA4・問い合わせの対象期間）= 前週の月〜日（日本時間）。

    week_start（月曜）を指定した場合はその週。実行日ではなく週で決めるため、
    月〜水のどの日に再実行しても同じ週になる（二重生成・二重配信の防止）。
    """
    if week_start is None:
        today = today or datetime.now(JST).date()
        week_start = today - timedelta(days=today.weekday() + 7)
    if week_start.weekday() != 0:
        raise ValueError(f"週の開始日は月曜日で指定する: {week_start}")
    week_end = week_start + timedelta(days=6)
    return {
        "report_week_start": week_start.isoformat(), "report_week_end": week_end.isoformat(),
        "ga4_prv_start": (week_start - timedelta(days=7)).isoformat(),
        "ga4_prv_end": (week_start - timedelta(days=1)).isoformat(),
    }


def compute_gsc_end(preferred_end: date, first_incomplete: date | None, last_date: date | None) -> date:
    """GSCの対象期間の末日 = min(希望末日, 確定済みの最新日)。

    first_incomplete: Search Console API（dataState=all）の metadata.firstIncompleteDate。
    これが無い場合は、返ってきた日付がすべて確定済み（last_date が確定済みの最新日）。
    """
    if first_incomplete is not None:
        latest_final = first_incomplete - timedelta(days=1)
    elif last_date is not None:
        latest_final = last_date
    else:
        raise GscFetchError("Search Consoleの確定日を判定できない（日付データが返らなかった）")
    return min(preferred_end, latest_final)


def fetch_gsc_freshness(service, site_url: str, start: date, end: date) -> tuple[date | None, date | None]:
    """(firstIncompleteDate, 返ってきた最新日) を返す。"""
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date"], "dataState": "all"}
    try:
        resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
    except HttpError as e:
        raise GscFetchError(f"Search Consoleの確定日の取得に失敗: {e}") from e
    first_incomplete = (resp.get("metadata") or {}).get("firstIncompleteDate")
    dates = [row["keys"][0] for row in resp.get("rows", [])]
    return (
        date.fromisoformat(first_incomplete) if first_incomplete else None,
        date.fromisoformat(max(dates)) if dates else None,
    )


def get_gsc_period(report_week: dict, first_incomplete: date | None, last_date: date | None,
                   pipeline_cfg: dict) -> dict:
    """GSCの対象期間 = 確定済みの最新7日間（mode=auto）。

    希望末日はレポート週の日曜から end_offset_days 日前（既定2＝金曜）。曜日構成を毎週そろえ、
    再実行日によって期間が変わらないようにする。金曜が未確定の週は確定済みの最新日まで前にずらし、
    gsc_shift_days に記録する（レポートに注記する）。
    """
    cutoff_cfg = pipeline_cfg["gsc"]["data_cutoff"]
    length_days = pipeline_cfg["gsc"]["report_period"]["length_days"]
    preferred_end = date.fromisoformat(report_week["report_week_end"]) - timedelta(days=cutoff_cfg["end_offset_days"])
    cur_end = compute_gsc_end(preferred_end, first_incomplete, last_date)
    max_shift = cutoff_cfg.get("max_shift_days", 6)
    if (preferred_end - cur_end).days > max_shift:
        raise GscFetchError(
            f"Search Consoleの確定が大幅に遅れている（確定済みの最新日 {cur_end}、希望末日 {preferred_end}）。"
            "期間がレポート週と大きくずれるため生成しない")
    cur_start = cur_end - timedelta(days=length_days - 1)
    prv_end = cur_start - timedelta(days=1)
    prv_start = prv_end - timedelta(days=length_days - 1)
    return {
        "cur_start": cur_start.isoformat(), "cur_end": cur_end.isoformat(),
        "prv_start": prv_start.isoformat(), "prv_end": prv_end.isoformat(),
        "gsc_preferred_end": preferred_end.isoformat(),
        "gsc_shift_days": (preferred_end - cur_end).days,
        "gsc_first_incomplete_date": first_incomplete.isoformat() if first_incomplete else None,
    }


def determine_period(service, pipeline_cfg: dict, week_start: date | None = None,
                     now: datetime | None = None) -> dict:
    """レポートの期間一式。

    cur_start〜prv_end は従来どおり GSC の期間（分析ロジックはこれを使う）。
    report_week_* は GA4・問い合わせの期間、ga4_prv_* はGA4の比較期間。
    """
    now = now or datetime.now(JST)
    mode = pipeline_cfg["gsc"]["data_cutoff"]["mode"]
    if mode != "auto":
        raise NotImplementedError(f"gsc.data_cutoff.mode='{mode}' では週次自動運用の期間を決められない（auto を使う）")
    report_week = get_report_week(today=now.date(), week_start=week_start)
    week_end = date.fromisoformat(report_week["report_week_end"])
    if week_end >= now.date():
        raise ValueError(f"レポート週がまだ終わっていない: {report_week['report_week_start']}〜{week_end}")
    first_incomplete, last_date = fetch_gsc_freshness(
        service, SITE_URL, week_end - timedelta(days=20), now.date())
    gsc = get_gsc_period(report_week, first_incomplete, last_date, pipeline_cfg)
    return {**gsc, **report_week, "period_scheme": "weekly_v3"}


def run(credentials_file: str = "credentials.json", output_root: Path | str = PROCESSED_DIR,
        week_start: date | None = None) -> Path:
    pages = config_loader.load_pages()
    queries_cfg = config_loader.load_queries()
    # aio: false の市場（参考クエリ）はAIO観測対象外（2026-09-30。SerpAPI件数は4件のまま）
    priority_queries = [m["query"] for m in queries_cfg["markets"].values() if m.get("aio", True)]
    pipeline_cfg = config_loader.load_pipeline_config()

    try:
        service = get_search_console_service(credentials_file)
        period = determine_period(service, pipeline_cfg, week_start=week_start)
    except GscFetchError as e:
        print(f"GSCの確定日を判定できないためレポートを生成しません: {e}", file=sys.stderr)
        raise
    out_dir = Path(output_root) / f"{period['report_week_start']}_{period['report_week_end']}"

    print(f"レポート週（GA4・問い合わせ）: {period['report_week_start']} 〜 {period['report_week_end']}")
    print(f"GSC対象: {period['cur_start']} 〜 {period['cur_end']}（比較 {period['prv_start']} 〜 {period['prv_end']}）")
    if period["gsc_shift_days"]:
        print(f"  警告: GSCの確定が遅れているため、末日を{period['gsc_shift_days']}日前にずらした"
              f"（希望末日 {period['gsc_preferred_end']}、未確定の最初の日 {period['gsc_first_incomplete_date']}）")

    print("Search Console データ取得中...")
    try:
        gsc_current = fetch_gsc.fetch_period(service, SITE_URL, period["cur_start"], period["cur_end"])
        gsc_comparison = fetch_gsc.fetch_period(service, SITE_URL, period["prv_start"], period["prv_end"])
    except GscFetchError as e:
        print(f"GSC取得に失敗したためレポートを生成しません: {e}", file=sys.stderr)
        raise

    print("AIO観測データ取得中...")
    period["aio_observed_at"] = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    aio_records = fetch_aio.fetch_priority_queries(priority_queries)

    # GA4の集計対象ホスト名を期間情報に記録する（前週の速報値と比べるとき、条件が同じかを確かめるため）
    period["ga4_hostnames"] = list(pipeline_cfg["ga4"].get("hostnames") or [])
    print("GA4サイトアクセス状況取得中...")
    ga4_segments = fetch_ga4.fetch_segments(
        pipeline_cfg["ga4"],
        period["report_week_start"], period["report_week_end"],
        period["ga4_prv_start"], period["ga4_prv_end"],
        credentials_file=credentials_file,
    )
    for key, seg in ga4_segments.items():
        if seg["error"]:
            print(f"  警告: GA4セグメント'{key}'の取得に失敗（参考情報のため処理は継続）: {seg['error']}")

    print("問い合わせ集計（スプレッドシート）取得中...")
    inquiries_snapshot = fetch_inquiries.fetch_snapshot(pipeline_cfg.get("inquiries"), credentials_file)
    if inquiries_snapshot["status"] != fetch_inquiries.SNAPSHOT_OK:
        print(f"  警告: 問い合わせ集計を取得できない（レポートは「未集計」と表示して継続）: {inquiries_snapshot['error']}")

    print("データ検証中...")
    try:
        data_quality = validate.run_validation(
            gsc_current, gsc_comparison, aio_records, priority_queries, period,
            manifest_path=Path(output_root) / "_last_run_manifest.json",
        )
    except DataQualityError as e:
        out_dir.mkdir(parents=True, exist_ok=True)
        dq_path = out_dir / "data_quality_report.json"
        dq_path.write_text(json.dumps(e.report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"データ品質検証がfailと判定されたためレポートを生成しません: {dq_path}", file=sys.stderr)
        raise

    print(f"データ品質: {data_quality['status']}")
    for w in data_quality["warnings"]:
        print(f"  警告: {w}")

    print("正規化中...")
    gsc_current_tables = normalize.build_gsc_processed_tables(gsc_current, pages)
    gsc_comparison_tables = normalize.build_gsc_processed_tables(gsc_comparison, pages)
    aio_table = normalize.build_aio_processed_table(aio_records, queries_cfg.get("competitor_domains"))

    bundle = {
        "schema_version": SCHEMA_VERSION,
        "period": period,
        "data_quality": data_quality,
        "gsc": {"current": gsc_current_tables, "comparison": gsc_comparison_tables},
        "aio_observations": aio_table,
        "ga4": ga4_segments,
        "inquiries_snapshot": inquiries_snapshot,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "normalized.json"
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"正規化済みデータ出力: {out_path}")

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD週次SEO/AIOレポート v2 Phase 2パイプライン")
    parser.add_argument("--credentials-file", default="credentials.json")
    parser.add_argument("--week", help="レポート週の月曜日（YYYY-MM-DD）。省略時は前週")
    parser.add_argument("--output-root", default=str(PROCESSED_DIR))
    args = parser.parse_args()
    week_start = date.fromisoformat(args.week) if args.week else None

    try:
        run(credentials_file=args.credentials_file, output_root=args.output_root, week_start=week_start)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    except (GscFetchError, DataQualityError):
        sys.exit(1)


if __name__ == "__main__":
    main()
