#!/usr/bin/env python3
"""ページ単体の検索クエリをスポット確認するCLI（週次レポートとは独立に実行できる）。

使い方:
  python3 scripts/fetch_page_queries.py
  python3 scripts/fetch_page_queries.py --url https://tcd.jp/branding/03-news/osaka-branding-company.html
  python3 scripts/fetch_page_queries.py --url <URL> --days 90 --current-days 7
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import generate_report as gr
import page_query_analysis as pqa


def main() -> None:
    parser = argparse.ArgumentParser(description="ページ単体の検索クエリ分析（地域ページ判定・AIOハブ候補判定）")
    parser.add_argument("--config", default="config/aio_monitor.yml")
    parser.add_argument("--credentials-file", default="credentials.json")
    parser.add_argument("--url", default=None, help="対象ページURL（未指定時は config の page_query_watch.pages 全件）")
    parser.add_argument("--current-days", type=int, default=7, help="直近期間の日数")
    parser.add_argument("--days", type=int, default=90, help="参考期間（長期）の日数")
    parser.add_argument("--output-dir", default="data/gsc")
    args = parser.parse_args()

    config = gr.load_config(args.config)
    site_url = config["site_url"]
    pqw_cfg = config.get("page_query_watch", {})
    cls_cfg = pqw_cfg.get("classification", {})
    regional_terms = cls_cfg.get("regional_terms", [])
    comparison_terms = cls_cfg.get("comparison_terms", [])
    nl_min_length = cls_cfg.get("natural_language_min_length", 20)

    if args.url:
        pages = [{"slug": "adhoc", "label": args.url, "url": args.url}]
    else:
        pages = pqw_cfg.get("pages", [])
        if not pages:
            print("対象ページがありません。--url を指定するか config/aio_monitor.yml の page_query_watch.pages を設定してください。", file=sys.stderr)
            sys.exit(1)

    service = gr.get_service(args.credentials_file)

    end = datetime.today() - timedelta(days=3)  # データラグ
    cur_end = end
    cur_start = cur_end - timedelta(days=args.current_days - 1)
    prv_end = cur_start - timedelta(days=1)
    prv_start = prv_end - timedelta(days=args.current_days - 1)
    ref_start = cur_end - timedelta(days=args.days - 1)

    fmt = lambda d: d.strftime("%Y-%m-%d")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for page_cfg in pages:
        page_url = page_cfg["url"]
        label = page_cfg.get("label", page_url)
        slug = page_cfg.get("slug", "page")
        print(f"=== {label} ({page_url}) ===")

        def _df(s: str, e: str) -> pd.DataFrame:
            rows = gr._fetch_rows(
                service, site_url, s, e, ["query"],
                dimension_filter={"dimension": "page", "operator": "equals", "expression": page_url},
            )
            return gr._to_df(rows, ["query"])

        cur_df = _df(fmt(cur_start), fmt(cur_end))
        prv_df = _df(fmt(prv_start), fmt(prv_end))
        ref_df = _df(fmt(ref_start), fmt(cur_end))

        analysis = pqa.analyze_page_query(cur_df, prv_df, ref_df, regional_terms, comparison_terms, nl_min_length)
        analysis["label"] = label
        analysis["url"] = page_url
        analysis["reference_period"] = (fmt(ref_start), fmt(cur_end))

        print(f"今週 {fmt(cur_start)}〜{fmt(cur_end)} / 前週 {fmt(prv_start)}〜{fmt(prv_end)} / 参考{args.days}日 {fmt(ref_start)}〜{fmt(cur_end)}")
        print(f"クエリ数: 今週{len(cur_df)} 前週{len(prv_df)} 参考{len(ref_df)}")
        print(f"地域ページ判定: {analysis['regional']['status']}")
        print(f"AIOハブ候補判定: {analysis['aio_hub']['status']}")
        if analysis["rows"]:
            print(pd.DataFrame(pqa.build_comparison_records(analysis)).to_string(index=False))

        period_path = output_dir / f"{slug}_page_queries.csv"
        cmp_path = output_dir / f"{slug}_page_queries_weekly_comparison.csv"
        pd.DataFrame(pqa.build_period_records(analysis)).to_csv(period_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(pqa.build_comparison_records(analysis)).to_csv(cmp_path, index=False, encoding="utf-8-sig")
        print(f"CSV: {period_path}, {cmp_path}")
        print()


if __name__ == "__main__":
    main()
