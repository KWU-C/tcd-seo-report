"""① サイト全体のアクセス状況の集計（GA4、2026-11-23週〜）。

  - セグメント別の到達ユーザー（activeUsers）と前週比
  - 異常値の検出: 週の値が特定の1日に集中しているセグメント。原因は断定しない（流入元を推測で書かない）。
  - 流入元の区分別セッション数（広告／自然検索／直接／参照元／生成AI／その他・不明）
  - 推移（直近 N 週）: アクティブユーザー数・セッション数の全体と広告を除いた値

指標名（アクティブユーザー数／セッション数）は各項目の "metric" に持たせ、表示側で必ず明記する。
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from src_v2.ga4_channels import AI, CHANNEL_KEYS, CHANNEL_LABELS


def _pct(cur, prv) -> float | None:
    if cur is None or not prv:
        return None
    return (cur - prv) / prv * 100


def _days(start: str, end: str) -> list[str]:
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def detect_anomaly(daily: dict[str, int], start: str, end: str, week_users: int | None, cfg: dict) -> dict | None:
    """週の値が1日に集中しているか。集中していれば {date, day_users, days_total, week_users} を返す。"""
    if week_users is None or week_users < cfg["min_week_users"]:
        return None
    values = [(d, daily.get(d, 0)) for d in _days(start, end)]
    total = sum(v for _, v in values)
    if not total:
        return None
    peak_day, peak = max(values, key=lambda x: x[1])
    others = [v for d, v in values if d != peak_day]
    if peak / total < cfg["min_day_share"]:
        return None
    if peak < cfg["min_ratio_to_median"] * max(median(others) if others else 0, 1):
        return None
    return {"date": peak_day, "day_users": peak, "days_total": total, "week_users": week_users}


def _segments(bundle: dict, cfg: dict) -> list[dict]:
    period = bundle["period"]
    windows = {}
    if "report_week_start" in period:
        windows = {"current": (period["report_week_start"], period["report_week_end"]),
                   "comparison": (period["ga4_prv_start"], period["ga4_prv_end"])}
    out = []
    for key, seg in (bundle.get("ga4") or {}).items():
        cur, prv = seg.get("current"), seg.get("comparison")
        anomalies = []
        if seg.get("daily") and windows and not seg.get("error"):
            for which, (start, end) in windows.items():
                found = detect_anomaly(seg["daily"], start, end, seg.get(which), cfg)
                if found:
                    anomalies.append({"period": which, **found})
        out.append({
            "key": key, "label": seg.get("label", key), "rule_label": seg.get("rule_label"),
            "group": seg.get("group", "cv" if key in ("contact", "document_request") else "main"),
            "metric": seg.get("metric", "activeUsers"),
            "current": cur, "comparison": prv, "delta_pct": _pct(cur, prv),
            "error": seg.get("error"), "anomalies": anomalies,
        })
    return out


def _channels(channels: dict | None) -> dict | None:
    if not channels:
        return None
    if channels.get("status") != "ok":
        return {"status": channels.get("status"), "error": channels.get("error"), "rows": [], "total": None, "ai_services": []}
    agg = {k: {"current": 0, "comparison": 0, "engaged_current": 0, "engaged_comparison": 0} for k in CHANNEL_KEYS}
    ai_services: dict[str, dict] = {}
    for r in channels["rows"]:
        a = agg[r["channel"]]
        a["current"] += r["current"]["sessions"]
        a["comparison"] += r["comparison"]["sessions"]
        a["engaged_current"] += r["current"]["engaged_sessions"]
        a["engaged_comparison"] += r["comparison"]["engaged_sessions"]
        if r["channel"] == AI:
            s = ai_services.setdefault(r["ai_service"], {"service": r["ai_service"], "current": 0, "comparison": 0})
            s["current"] += r["current"]["sessions"]
            s["comparison"] += r["comparison"]["sessions"]
    total_cur = sum(a["current"] for a in agg.values())
    total_prv = sum(a["comparison"] for a in agg.values())
    rows = []
    for k in CHANNEL_KEYS:
        a = agg[k]
        rows.append({
            "key": k, "label": CHANNEL_LABELS[k], "current": a["current"], "comparison": a["comparison"],
            "delta_pct": _pct(a["current"], a["comparison"]),
            "share_pct": a["current"] / total_cur * 100 if total_cur else None,
            "engagement_rate": a["engaged_current"] / a["current"] * 100 if a["current"] else None,
        })
    # 参照元ごとの上位（区分の中身を確かめるため。セッション数の多い順に各区分3件まで）
    top_sources: dict[str, list[dict]] = {}
    for r in sorted(channels["rows"], key=lambda r: -r["current"]["sessions"]):
        lst = top_sources.setdefault(r["channel"], [])
        if len(lst) < 3 and r["current"]["sessions"]:
            lst.append({"source_medium": r["source_medium"], "current": r["current"]["sessions"]})
    return {
        "status": "ok", "error": None, "metric": channels.get("metric", "sessions"), "rows": rows,
        "total": {"current": total_cur, "comparison": total_prv, "delta_pct": _pct(total_cur, total_prv)},
        "ai_services": sorted(ai_services.values(), key=lambda s: (-s["current"], s["service"])),
        "top_sources": top_sources,
    }


def build_access_summary(bundle: dict, thresholds_cfg: dict) -> dict:
    cfg = thresholds_cfg["access_anomaly"]
    access = bundle.get("ga4_access") or {}
    return {
        "version": access.get("version"),
        "segments": _segments(bundle, cfg),
        "channels": _channels(access.get("channels")),
        "trend": access.get("trend"),
    }
