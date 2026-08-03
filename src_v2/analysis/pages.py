"""config_v2/pages.yaml を基準としたページ分析。

「マガジン上位ランディングページ」（動的抽出対象）はここでは扱わない
（pages.yamlに含まれないため。将来、動的抽出結果を分析する場合は
別モジュールで扱う）。
"""
from __future__ import annotations

from src_v2 import normalize
from src_v2.analysis.common import group_metrics, make_finding, position_delta, sum_metrics, top_n_by
from src_v2.analysis.confidence import assess_confidence

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _escalate(current: str, at_least: str) -> str:
    return current if _SEVERITY_ORDER[current] >= _SEVERITY_ORDER[at_least] else at_least


def _aio_cites_page(normalized_page_url: str, aio_observations: list[dict]) -> list[str]:
    citing_queries = []
    for obs in aio_observations:
        if obs.get("acquisition_status") != "success":
            continue
        for url in obs.get("tcd_urls") or []:
            if normalize.normalize_url(url)["normalized_url"] == normalized_page_url:
                citing_queries.append(obs["query"])
                break
    return citing_queries


def analyze_pages(
    bundle: dict,
    pages_cfg: list[dict],
    thresholds_cfg: dict,
    weeks_available: int = 1,
) -> list[dict]:
    findings: list[dict] = []

    page_daily_cur = bundle["gsc"]["current"]["gsc_page_daily"]
    page_daily_prv = bundle["gsc"]["comparison"]["gsc_page_daily"]
    page_query_cur = bundle["gsc"]["current"]["gsc_page_query_daily"]
    aio_observations = bundle.get("aio_observations", [])

    low_impressions = thresholds_cfg["low_data"]["low_impressions"]
    low_clicks = thresholds_cfg["low_data"]["low_clicks"]
    meaningful = thresholds_cfg["low_data"]["position_change_meaningful"]

    for page in pages_cfg:
        if not page.get("enabled"):
            continue

        page_id = page["id"]
        normalized_page_url = normalize.normalize_url(page["url"])["normalized_url"]

        cur_rows = [r for r in page_daily_cur if r["page_id"] == page_id]
        prv_rows = [r for r in page_daily_prv if r["page_id"] == page_id]
        cur = sum_metrics(cur_rows)
        prv = sum_metrics(prv_rows)

        top_queries_rows = [r for r in page_query_cur if r["page_id"] == page_id]
        top_queries = top_n_by(group_metrics(top_queries_rows, key_fn=lambda r: r["query"]), n=5)

        citing_queries = _aio_cites_page(normalized_page_url, aio_observations)

        if not cur_rows and not prv_rows:
            severity, judgment = "warning", "再調査"
            message = "Search Consoleデータで検出されない（表示回数ゼロ、またはインデックス未確認）"
            recommendation = "対象URLがインデックスされているか、正しいURLかを確認する"
        elif (cur["impressions"] or 0) < low_impressions and (cur["clicks"] or 0) < low_clicks:
            severity, judgment = "info", "判断保留"
            message = f"表示回数・クリック数が少なく参考値（impressions={cur['impressions']}, clicks={cur['clicks']}）"
            recommendation = "データ蓄積を待って再評価する"
        else:
            delta = position_delta(cur["position"], prv.get("position"))
            if delta is not None and delta <= -meaningful:
                severity, judgment = "warning", "再調査"
                message = f"順位が下落（{prv.get('position'):.1f}→{cur['position']:.1f}位）"
                recommendation = "ページ×クエリで下落クエリを特定／検索結果のタイトル・descriptionを競合と比較／内部リンクを確認"
            elif delta is not None and delta >= meaningful:
                severity, judgment = "info", "継続観測"
                message = f"順位が改善（{prv.get('position'):.1f}→{cur['position']:.1f}位）"
                recommendation = "改善傾向を継続観測する"
            else:
                severity, judgment = "info", "継続観測"
                position_str = f"{cur['position']:.1f}位" if cur["position"] is not None else "順位不明"
                message = f"おおむね横ばい（{position_str}）"
                recommendation = "継続観測する"

        objective = page.get("objective") or []
        if "conversion" in objective and cur["impressions"] > 0 and cur["clicks"] == 0:
            severity = _escalate(severity, "warning")
            message += "／コンバージョン導線ページだが今週クリックがない"

        if citing_queries:
            message += f"／AIOで引用あり（{', '.join(citing_queries)}）"

        confidence = assess_confidence({
            "impressions": cur["impressions"] if cur_rows else None,
            "clicks": cur["clicks"] if cur_rows else None,
            "weeks_available": weeks_available,
            "seo_aio_agree": None,
            "page_query_agree": None,
        }, thresholds_cfg)

        findings.append(make_finding(
            id=f"page-{page_id}",
            category="page",
            severity=severity,
            confidence=confidence["level"],
            message=message,
            recommendation=recommendation,
            judgment=judgment,
            evidence={
                "page_id": page_id, "title": page.get("title"), "url": page["url"], "group": page["group"],
                "category": page["category"], "objective": objective,
                "current": cur, "comparison": prv,
                "top_queries": [{"query": q, **m} for q, m in top_queries],
                "aio_citing_queries": citing_queries,
            },
            source={"page_id": page_id, "url": page["url"]},
        ))

    return findings
