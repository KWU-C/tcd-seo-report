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
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src_v2 import config_loader, fetch_aio, fetch_ga4, fetch_gsc, normalize, validate
from src_v2.auth import get_search_console_service
from src_v2.schema import SCHEMA_VERSION, DataQualityError, GscFetchError

PROCESSED_DIR = Path("data_v2/processed")
SITE_URL = "https://tcd.jp/"


def get_weekly_period(today: datetime | None = None, pipeline_cfg: dict | None = None) -> dict:
    """確定日カットオフ設定(config_v2/pipeline.yaml)に基づき、対象7日間と比較7日間を返す。

    mode=fixed: 実行日からoffset_days日前を対象期間の末日とする
    （Search Consoleの遅延を確実にカバーするため。曜日には依存しない）。
    """
    today = today or datetime.today()
    pipeline_cfg = pipeline_cfg or config_loader.load_pipeline_config()
    fmt = lambda d: d.strftime("%Y-%m-%d")

    cutoff_cfg = pipeline_cfg["gsc"]["data_cutoff"]
    mode = cutoff_cfg["mode"]
    if mode != "fixed":
        raise NotImplementedError(f"gsc.data_cutoff.mode='{mode}' は未実装（v3以降でauto対応予定）")

    length_days = pipeline_cfg["gsc"]["report_period"]["length_days"]

    cur_end = today - timedelta(days=cutoff_cfg["offset_days"])
    cur_start = cur_end - timedelta(days=length_days - 1)
    prv_end = cur_start - timedelta(days=1)
    prv_start = prv_end - timedelta(days=length_days - 1)

    return {
        "cur_start": fmt(cur_start), "cur_end": fmt(cur_end),
        "prv_start": fmt(prv_start), "prv_end": fmt(prv_end),
    }


def run(credentials_file: str = "credentials.json", output_root: Path | str = PROCESSED_DIR) -> Path:
    pages = config_loader.load_pages()
    queries_cfg = config_loader.load_queries()
    priority_queries = [m["query"] for m in queries_cfg["markets"].values()]
    pipeline_cfg = config_loader.load_pipeline_config()

    period = get_weekly_period(pipeline_cfg=pipeline_cfg)
    out_dir = Path(output_root) / f"{period['cur_start']}_{period['cur_end']}"

    print(f"対象週: {period['cur_start']} 〜 {period['cur_end']}")
    print(f"比較週: {period['prv_start']} 〜 {period['prv_end']}")

    print("Search Console データ取得中...")
    try:
        service = get_search_console_service(credentials_file)
        gsc_current = fetch_gsc.fetch_period(service, SITE_URL, period["cur_start"], period["cur_end"])
        gsc_comparison = fetch_gsc.fetch_period(service, SITE_URL, period["prv_start"], period["prv_end"])
    except GscFetchError as e:
        print(f"GSC取得に失敗したためレポートを生成しません: {e}", file=sys.stderr)
        raise

    print("AIO観測データ取得中...")
    aio_records = fetch_aio.fetch_priority_queries(priority_queries)

    print("GA4サイトアクセス状況取得中...")
    ga4_segments = fetch_ga4.fetch_segments(
        pipeline_cfg["ga4"],
        period["cur_start"], period["cur_end"],
        period["prv_start"], period["prv_end"],
        credentials_file=credentials_file,
    )
    for key, seg in ga4_segments.items():
        if seg["error"]:
            print(f"  警告: GA4セグメント'{key}'の取得に失敗（参考情報のため処理は継続）: {seg['error']}")

    print("データ検証中...")
    try:
        data_quality = validate.run_validation(
            gsc_current, gsc_comparison, aio_records, priority_queries, period,
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
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "normalized.json"
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"正規化済みデータ出力: {out_path}")

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD週次SEO/AIOレポート v2 Phase 2パイプライン")
    parser.add_argument("--credentials-file", default="credentials.json")
    args = parser.parse_args()

    try:
        run(credentials_file=args.credentials_file)
    except (GscFetchError, DataQualityError):
        sys.exit(1)


if __name__ == "__main__":
    main()
