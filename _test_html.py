#!/usr/bin/env python3
"""
SC API なしで gen_html() をローカルテストするスクリプト。
reports/google_aio/2026-06-08.json のキャッシュと
2026-06-01-weekly-seo-aio-report-combined.md の実績値を使用。
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from generate_report import gen_html, load_config, aio_competitor_ranking

config = load_config("config/aio_monitor.yml")

with open("reports/google_aio/2026-06-08.json", encoding="utf-8") as f:
    google_aio = json.load(f)

# 2026-06-01-weekly-seo-aio-report-combined.md の実績値
data = {
    "periods": (("2026-06-01", "2026-06-07"), ("2026-05-25", "2026-05-31")),
    "period_type": "weekly",
    "totals": {
        "current":  {"clicks": 178, "impressions": 8167, "ctr": 0.0218, "position": 9.6},
        "previous": {"clicks": 235, "impressions": 10690, "ctr": 0.0220, "position": 11.0},
    },
    "key_kws": [
        {"query": "ブランディング 会社",
         "cur": {"clicks": 1, "impressions": 63, "ctr": 0.016, "position": 16.7},
         "prv": {"clicks": 1, "impressions": 60, "ctr": 0.017, "position": 17.0},
         "click_chg": 0.0, "pos_chg": 0.3, "ctr_chg": -0.001},
        {"query": "ブランディング 会社 東京",
         "cur": {"clicks": 0, "impressions": 76, "ctr": 0.0, "position": 8.8},
         "prv": {"clicks": 2, "impressions": 80, "ctr": 0.025, "position": 8.5},
         "click_chg": -100.0, "pos_chg": 0.3, "ctr_chg": -0.025},
        {"query": "インナーブランディング 会社 東京",
         "cur": {"clicks": 0, "impressions": 13, "ctr": 0.0, "position": 17.8},
         "prv": {"clicks": 1, "impressions": 15, "ctr": 0.067, "position": 16.5},
         "click_chg": -100.0, "pos_chg": 1.3, "ctr_chg": -0.067},
    ],
    "service_pages": [],
    "aio_pages": [],
    "def_pages": [],
    "lp_pages": [
        {"name": "トップページ", "title": "トップページ", "url": "https://tcd.jp/",
         "cur": {"clicks": 30, "impressions": 500, "ctr": 0.06, "position": 5.2},
         "prv": {"clicks": 40, "impressions": 600, "ctr": 0.067, "position": 5.0},
         "click_chg": -25.0, "pos_chg": 0.2, "ctr_chg": -0.007, "aio_score": 0.1},
    ],
    "devices": [
        {"device": "mobile", "clicks": 110, "impressions": 5200, "ctr": 0.021, "position": 9.8},
        {"device": "desktop", "clicks": 65, "impressions": 2800, "ctr": 0.023, "position": 9.2},
        {"device": "tablet", "clicks": 3, "impressions": 167, "ctr": 0.018, "position": 10.1},
    ],
    "aio_anomalies": [],
    "top_queries": [],
    "top_pages": [],
    "google_aio": google_aio,
}

html = gen_html(data, config)
out = Path("reports/test_aio_ranking.html")
out.write_text(html, encoding="utf-8")
print(f"生成完了: {out}")
ranking, total_aio = aio_competitor_ranking(google_aio)
print(f"観測AIO: {total_aio}件")
for r in ranking:
    print(f"  {r['name']}: {r['count']}件 ({r['share']:.0f}%)")
