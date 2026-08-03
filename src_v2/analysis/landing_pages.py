"""マガジン（/branding/）上位ランディングページの動的抽出。

requirements.md 4節: 「マガジン上位ランディングページ」は固定リストではなく、
config_v2/pages.yaml（定点観測マスタ）には含めない。毎週のSearch Console
データから動的に抽出する。

抽出条件:
  - normalized_url が /branding/ 配下
  - page_id が解決されない（＝pages.yamlの定点観測対象ではない）
  - config_v2/excluded_landing_pages.yaml の除外リストに載っていない
上記を満たす候補をクリック数降順でtop_n件（既定10件）抽出する。
"""
from __future__ import annotations

import hashlib

from src_v2 import normalize
from src_v2.analysis.common import group_metrics, make_finding, top_n_by
from src_v2.analysis.confidence import assess_confidence

BRANDING_PATH_PREFIX = "https://tcd.jp/branding/"


def _excluded_url_set(excluded_cfg: dict | None) -> set[str]:
    urls = [e["url"] for e in (excluded_cfg or {}).get("excluded_urls", [])]
    return {normalize.normalize_url(u)["normalized_url"] for u in urls}


def _slug(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def analyze_landing_pages(
    bundle: dict,
    excluded_cfg: dict | None,
    thresholds_cfg: dict,
    weeks_available: int = 1,
    top_n: int = 10,
) -> list[dict]:
    page_daily_cur = bundle["gsc"]["current"]["gsc_page_daily"]
    page_daily_prv = bundle["gsc"]["comparison"]["gsc_page_daily"]
    excluded_urls = _excluded_url_set(excluded_cfg)

    candidate_rows = [
        r for r in page_daily_cur
        if r["normalized_url"].startswith(BRANDING_PATH_PREFIX)
        and r.get("page_id") is None
        and r["normalized_url"] not in excluded_urls
    ]
    if not candidate_rows:
        return []

    grouped_cur = group_metrics(candidate_rows, key_fn=lambda r: r["normalized_url"])
    ranked = top_n_by(grouped_cur, n=top_n, sort_key="clicks")
    ranked_urls = {url for url, _ in ranked}

    prv_rows = [r for r in page_daily_prv if r["normalized_url"] in ranked_urls]
    grouped_prv = group_metrics(prv_rows, key_fn=lambda r: r["normalized_url"])

    findings: list[dict] = []
    for rank, (url, cur_metrics) in enumerate(ranked, start=1):
        prv_metrics = grouped_prv.get(url, {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": None})

        confidence = assess_confidence({
            "impressions": cur_metrics["impressions"],
            "clicks": cur_metrics["clicks"],
            "weeks_available": weeks_available,
            "seo_aio_agree": None,
            "page_query_agree": None,
        }, thresholds_cfg)

        findings.append(make_finding(
            id=f"landing-{rank}-{_slug(url)}",
            category="magazine_landing_page",
            severity="info",
            confidence=confidence["level"],
            message=f"第{rank}位: クリック{cur_metrics['clicks']}件・表示{cur_metrics['impressions']}件",
            recommendation="動向を継続観測する（定点観測対象への追加候補として検討）",
            judgment="継続観測",
            evidence={"url": url, "rank": rank, "current": cur_metrics, "comparison": prv_metrics},
            source={"url": url},
        ))

    return findings
