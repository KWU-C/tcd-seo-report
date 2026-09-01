"""ページ単体クエリ分析（地域ページ判定・AIOハブ候補判定）。

Search Console の page フィルタで絞り込んだクエリ一覧を分類・比較する。
GSC API 呼び出しは含まない（generate_report.py 側で取得したDataFrameを渡す）。
"""
from __future__ import annotations

import pandas as pd


def classify_query(
    query: str,
    regional_terms: list[str],
    comparison_terms: list[str],
    nl_min_length: int = 20,
) -> str:
    """クエリを regional / natural_language / comparison_selection / general に分類する。"""
    if any(t in query for t in regional_terms):
        return "regional"
    if "？" in query or "?" in query or len(query) >= nl_min_length:
        return "natural_language"
    if any(t in query for t in comparison_terms):
        return "comparison_selection"
    return "general"


def classify_df(
    df: pd.DataFrame,
    regional_terms: list[str],
    comparison_terms: list[str],
    nl_min_length: int = 20,
) -> list[dict]:
    if df.empty:
        return []
    rows = [
        {
            "query": r["query"],
            "clicks": int(r["clicks"]),
            "impressions": int(r["impressions"]),
            "ctr": float(r["ctr"]),
            "position": float(r["position"]),
            "classification": classify_query(r["query"], regional_terms, comparison_terms, nl_min_length),
        }
        for _, r in df.iterrows()
    ]
    rows.sort(key=lambda x: -x["impressions"])
    return rows


def merge_comparison(cur_rows: list[dict], prv_rows: list[dict]) -> list[dict]:
    """今週のクエリ一覧に前週値・差分・新規フラグを付与する。"""
    prv_by_query = {r["query"]: r for r in prv_rows}
    merged = []
    for r in cur_rows:
        prv = prv_by_query.get(r["query"])
        prv_impr = prv["impressions"] if prv else 0
        prv_pos = prv["position"] if prv else None
        merged.append({
            **r,
            "prv_impressions": prv_impr,
            "prv_position": prv_pos,
            "impressions_diff": r["impressions"] - prv_impr,
            "position_diff": (r["position"] - prv_pos) if prv_pos is not None else None,
            "is_new_query": prv is None,
        })
    return merged


def _totals(rows: list[dict]) -> dict:
    if not rows:
        return {"query_count": 0, "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    clicks = sum(r["clicks"] for r in rows)
    impressions = sum(r["impressions"] for r in rows)
    ctr = clicks / impressions if impressions else 0.0
    weighted_pos = sum(r["position"] * r["impressions"] for r in rows)
    position = weighted_pos / impressions if impressions else 0.0
    return {"query_count": len(rows), "clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}


def evaluate_regional_recognition(merged_rows: list[dict], ref_rows: list[dict]) -> dict:
    """大阪・関西など地域クエリでの表示状況から「地域ページ」認識度を評価する。"""
    cur = [r for r in merged_rows if r["classification"] == "regional"]
    ref = [r for r in ref_rows if r["classification"] == "regional"]

    query_count = len(cur)
    impressions = sum(r["impressions"] for r in cur)
    prv_impressions = sum(r["prv_impressions"] for r in cur)
    prv_query_count = len([r for r in cur if r["prv_impressions"] > 0])
    ref_query_count = len(ref)

    if query_count == 0:
        status = "none"
    elif query_count <= 1 or ref_query_count <= 1:
        status = "weak"
    else:
        status = "confirmed"
        if impressions > prv_impressions or query_count > prv_query_count:
            status = "strengthening"

    return {
        "status": status,
        "query_count": query_count,
        "impressions": impressions,
        "prv_impressions": prv_impressions,
        "ref_query_count": ref_query_count,
        "new_queries": [r["query"] for r in cur if r["is_new_query"]],
        "moved_queries": [r for r in cur if r["position_diff"] is not None and abs(r["position_diff"]) >= 3],
        "rows": cur,
    }


def evaluate_aio_hub_growth(merged_rows: list[dict], ref_rows: list[dict]) -> dict:
    """地域名を含まない一般語・比較語・自然文クエリの広がりからAIOハブ候補としての段階を評価する。"""
    general = [r for r in merged_rows if r["classification"] == "general"]
    comparison = [r for r in merged_rows if r["classification"] == "comparison_selection"]
    natural_language = [r for r in merged_rows if r["classification"] == "natural_language"]
    non_regional = general + comparison + natural_language

    general_ref = [r for r in ref_rows if r["classification"] == "general"]

    has_general = len(general) > 0
    has_signal = len(comparison) > 0 or len(natural_language) > 0

    if not non_regional:
        status = "none"
    elif has_general and has_signal:
        status = "initial_signal"
    elif has_general or has_signal:
        status = "initial_signal" if (has_general or len(ref_rows) == 0) else "none"
    else:
        status = "none"

    if status == "initial_signal":
        base = general + comparison
        cur_impr = sum(r["impressions"] for r in base)
        prv_impr = sum(r["prv_impressions"] for r in base)
        avg_pos = (
            sum(r["position"] * r["impressions"] for r in general) / sum(r["impressions"] for r in general)
            if general and sum(r["impressions"] for r in general) > 0 else None
        )
        if cur_impr > prv_impr and avg_pos is not None and avg_pos <= 20:
            status = "growing"

    if status == "growing":
        clicks = sum(r["clicks"] for r in non_regional)
        high_rank = [r for r in comparison + natural_language if r["position"] <= 10]
        established_general = [r for r in general_ref if r["impressions"] > 0]
        if clicks > 0 and len(high_rank) >= 2 and len(established_general) >= 1:
            status = "established"

    return {
        "status": status,
        "general_rows": general,
        "comparison_rows": comparison,
        "natural_language_rows": natural_language,
        "new_queries": [r["query"] for r in non_regional if r["is_new_query"]],
    }


def analyze_page_query(
    cur_df: pd.DataFrame,
    prv_df: pd.DataFrame,
    ref_df: pd.DataFrame,
    regional_terms: list[str],
    comparison_terms: list[str],
    nl_min_length: int = 20,
) -> dict:
    cur_rows = classify_df(cur_df, regional_terms, comparison_terms, nl_min_length)
    prv_rows = classify_df(prv_df, regional_terms, comparison_terms, nl_min_length)
    ref_rows = classify_df(ref_df, regional_terms, comparison_terms, nl_min_length)
    merged = merge_comparison(cur_rows, prv_rows)

    return {
        "rows": merged,
        "cur_rows": cur_rows,
        "prv_rows": prv_rows,
        "ref_rows": ref_rows,
        "totals_current": _totals(cur_rows),
        "totals_previous": _totals(prv_rows),
        "regional": evaluate_regional_recognition(merged, ref_rows),
        "aio_hub": evaluate_aio_hub_growth(merged, ref_rows),
    }


def build_period_records(analysis: dict) -> list[dict]:
    """CSV出力用（長形式：期間ごとに1クエリ1行）。"""
    is_new_lookup = {r["query"]: r["is_new_query"] for r in analysis["rows"]}
    records: list[dict] = []

    def add(rows: list[dict], period: str, mark_new: bool) -> None:
        for r in rows:
            records.append({
                "period": period,
                "query": r["query"],
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "ctr": round(r["ctr"], 4),
                "position": round(r["position"], 2),
                "classification": r["classification"],
                "is_new_query": is_new_lookup.get(r["query"], False) if mark_new else False,
            })

    add(analysis["cur_rows"], "current_week", mark_new=True)
    add(analysis["prv_rows"], "previous_week", mark_new=False)
    add(analysis["ref_rows"], "reference_90d", mark_new=False)
    return records


def build_comparison_records(analysis: dict) -> list[dict]:
    """CSV出力用（広形式：今週×前週の比較）。"""
    return [
        {
            "query": r["query"],
            "clicks": r["clicks"],
            "impressions": r["impressions"],
            "ctr": round(r["ctr"], 4),
            "position": round(r["position"], 2),
            "prv_impressions": r["prv_impressions"],
            "impressions_diff": r["impressions_diff"],
            "prv_position": round(r["prv_position"], 2) if r["prv_position"] is not None else "",
            "position_diff": round(r["position_diff"], 2) if r["position_diff"] is not None else "",
            "classification": r["classification"],
            "is_new_query": r["is_new_query"],
        }
        for r in analysis["rows"]
    ]
