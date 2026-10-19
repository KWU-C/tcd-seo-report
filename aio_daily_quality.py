#!/usr/bin/env python3
"""日次AIO観測（data/aio/looker/aio_daily.csv）の判定不可の件数・割合を集計する。APIは使用しない。

  - TCD引用の判定不可: AIOはあるが本文・引用元を取得できなかった観測（citation_status=unavailable）。
    分母はAIOありの観測
  - TCD推薦の判定不可: 会社一覧を最後まで取得できたと判断できなかった観測（recommendation_status=undeterminable）。
    分母は推薦判定の対象クエリでAIOありの観測

使い方:
  python3 aio_daily_quality.py                         # 全期間（週別・クエリ別）
  python3 aio_daily_quality.py --since 2026-10-01      # 期間を絞る
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

import aio_daily_monitor as monitor

REASON_LABELS = {
    monitor.REASON_CONTENT_UNAVAILABLE: "本文・引用元を取得できず",
    monitor.REASON_NO_LIST: "会社一覧なし（要約文の社名のみ等）",
    monitor.REASON_HEADING_WITHOUT_ITEMS: "一覧の見出しのみで項目が欠落",
    monitor.REASON_TOO_FEW_ITEMS: "一覧項目が少なすぎる",
    monitor.REASON_LIST_NOT_CLOSED: "一覧の後に続く段落がない（途中で切れた可能性）",
}


def summarize(df: pd.DataFrame, by: str | None) -> pd.DataFrame:
    """by（列名）ごとに判定不可の件数・割合を返す。Noneなら全体1行。"""
    aio = df[df["aio_exists"].astype(str).str.lower() == "true"]
    target = aio[~aio["recommendation_status"].isin([monitor.NOT_APPLICABLE, monitor.NO_AIO]) & aio["recommendation_status"].notna()]
    groups = [("全体", aio, target)] if by is None else [
        (key, aio[aio[by] == key], target[target[by] == key]) for key in sorted(aio[by].dropna().unique())
    ]
    rows = []
    for key, a, t in groups:
        cite_unknown = int((a["citation_status"] == monitor.CITATION_UNAVAILABLE).sum())
        rec_unknown = int((t["recommendation_status"] == monitor.UNDETERMINABLE).sum())
        rows.append({
            by or "範囲": key,
            "AIOあり": len(a),
            "引用 判定不可": cite_unknown,
            "引用 判定不可率": f"{cite_unknown / len(a):.0%}" if len(a) else "-",
            "推薦 対象": len(t),
            "推薦 判定不可": rec_unknown,
            "推薦 判定不可率": f"{rec_unknown / len(t):.0%}" if len(t) else "-",
            "推薦あり": int((t["recommendation_status"] == monitor.RECOMMENDED).sum()),
            "推薦なし": int((t["recommendation_status"] == monitor.NOT_RECOMMENDED).sum()),
        })
    return pd.DataFrame(rows)


def reason_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    und = df[df["recommendation_status"] == monitor.UNDETERMINABLE]
    counts = und["recommendation_reason"].fillna("(理由未記録)").value_counts()
    return pd.DataFrame({"理由": [REASON_LABELS.get(k, k) for k in counts.index], "件数": counts.values})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="日次AIO観測の判定不可の件数・割合")
    parser.add_argument("--csv", default=str(Path(monitor.DEFAULT_LOOKER_DIR) / monitor.AIO_DAILY_CSV_NAME))
    parser.add_argument("--since", help="集計開始日（YYYY-MM-DD、含む）")
    parser.add_argument("--until", help="集計終了日（YYYY-MM-DD、含む）")
    args = parser.parse_args(argv)

    df = pd.read_csv(args.csv, dtype=str)
    for col in monitor.DAILY_CSV_COLUMNS:
        if col not in df.columns:
            df[col] = None
    if args.since:
        df = df[df["date"] >= args.since]
    if args.until:
        df = df[df["date"] <= args.until]
    if df.empty:
        print("対象期間のデータがありません。", file=sys.stderr)
        return 1

    dates = pd.to_datetime(df["date"])
    df = df.assign(週=(dates - pd.to_timedelta(dates.dt.weekday, unit="D")).dt.strftime("%Y-%m-%d週"))

    with pd.option_context("display.max_rows", None, "display.width", 200, "display.unicode.east_asian_width", True):
        print(f"期間: {df['date'].min()} 〜 {df['date'].max()}（{df['date'].nunique()}日分）\n")
        print(summarize(df, None).to_string(index=False), "\n")
        print("■ 週別"); print(summarize(df, "週").to_string(index=False), "\n")
        print("■ クエリ別"); print(summarize(df, "query").to_string(index=False), "\n")
        print("■ 推薦 判定不可の理由"); print(reason_breakdown(df).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
