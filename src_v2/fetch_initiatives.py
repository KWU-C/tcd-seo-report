"""施策の効果確認用の Search Console データ（実装前後の比較、2026-11-23週〜）。

docs/current-initiatives.md の各施策について、実装日の前後で同じ日数の期間を取り、対象ページの
表示回数・クリック数・平均掲載順位・検索クエリを取得する。あわせてサイト全体の日別値を取り、
対象ページ以外（＝比較対照）の変化と比べられるようにする。

期間（initiative_windows）:
  - 実装日当日は、公開前後のデータが混ざるため前後どちらにも入れない。
  - 実装後 = 実装日の翌日から、Search Console の確定済み最新日まで（最大 max_days 日。超える場合は直近 max_days 日）。
  - 実装前 = 実装日の前日までの、実装後と同じ日数。

施策の効果確認は Appendix の参考情報のため、取得に失敗してもレポート生成は止めない（施策ごとに error を記録）。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from googleapiclient.errors import HttpError

INITIATIVES_VERSION = 1
SITE_URL = "https://tcd.jp/"
_ROW_LIMIT = 25_000


def initiative_windows(implemented_at: date, gsc_end: date, max_days: int = 28) -> dict | None:
    """実装前後の比較期間。実装後のデータが1日も無ければ None。"""
    post_end = gsc_end
    post_start = max(implemented_at + timedelta(days=1), post_end - timedelta(days=max_days - 1))
    days = (post_end - post_start).days + 1
    if days <= 0:
        return None
    pre_end = implemented_at - timedelta(days=1)
    pre_start = pre_end - timedelta(days=days - 1)
    return {
        "pre": {"start": pre_start.isoformat(), "end": pre_end.isoformat()},
        "post": {"start": post_start.isoformat(), "end": post_end.isoformat()},
        "days": days,
        "elapsed_days": (gsc_end - implemented_at).days,
        "excluded_day": implemented_at.isoformat(),
    }


def page_regex(urls: list[str]) -> str:
    """対象URL（パスの完全一致。http/https・www の有無は問わない）の RE2 正規表現。"""
    paths = sorted({urlsplit(u).path or "/" for u in urls})
    return r"^https?://(www\.)?tcd\.jp(" + "|".join(re.escape(p) for p in paths) + r")$"


def _query(service: Any, body: dict) -> list[dict]:
    rows, start = [], 0
    while True:
        resp = service.searchanalytics().query(
            siteUrl=SITE_URL, body={**body, "rowLimit": _ROW_LIMIT, "startRow": start, "aggregationType": "byPage"}).execute()
        batch = resp.get("rows", [])
        rows += batch
        if len(batch) < _ROW_LIMIT:
            return rows
        start += len(batch)


def _filter(regex: str) -> list[dict]:
    return [{"filters": [{"dimension": "page", "operator": "includingRegex", "expression": regex}]}]


def _metrics(row: dict) -> dict:
    return {"clicks": int(row.get("clicks", 0)), "impressions": int(row.get("impressions", 0)),
            "position": row.get("position")}


def fetch_initiative(service: Any, urls: list[str], windows: dict) -> dict:
    """1施策分: 期間ごとのページ別・クエリ別の値と、日別の値（実装前の初日〜実装後の末日）。"""
    flt = _filter(page_regex(urls))
    out: dict[str, Any] = {}
    for key in ("pre", "post"):
        w = windows[key]
        pages = _query(service, {"startDate": w["start"], "endDate": w["end"], "dimensions": ["page"],
                                 "dimensionFilterGroups": flt})
        queries: dict[str, dict] = {}
        for row in _query(service, {"startDate": w["start"], "endDate": w["end"], "dimensions": ["page", "query"],
                                    "dimensionFilterGroups": flt}):
            q = queries.setdefault(row["keys"][1], {"clicks": 0, "impressions": 0, "position_x_imp": 0.0})
            q["clicks"] += int(row["clicks"])
            q["impressions"] += int(row["impressions"])
            q["position_x_imp"] += row["position"] * row["impressions"]
        out[key] = {
            "pages": {r["keys"][0]: _metrics(r) for r in pages},
            "queries": {k: {"clicks": v["clicks"], "impressions": v["impressions"],
                            "position": v["position_x_imp"] / v["impressions"] if v["impressions"] else None}
                        for k, v in queries.items()},
        }
    daily = _query(service, {"startDate": windows["pre"]["start"], "endDate": windows["post"]["end"],
                             "dimensions": ["date"], "dimensionFilterGroups": flt})
    out["daily"] = {r["keys"][0]: _metrics(r) for r in daily}
    return out


def fetch_all(service: Any, initiatives: list[dict], pages_cfg: list[dict], gsc_end: str, max_days: int = 28) -> dict:
    """全施策分。施策ごとの状態: ok / no_date（実装日なし・不正）/ no_target（対象ページを特定できない）/
    not_started（実装後のデータが無い）/ failed（取得失敗）。"""
    from src_v2.analysis.initiatives import parse_implemented_at, resolve_target_urls

    end = date.fromisoformat(gsc_end)
    items: dict[str, dict] = {}
    earliest: date | None = None
    for ini in initiatives:
        name = ini["name"]
        implemented_at = parse_implemented_at(ini)
        if implemented_at is None:
            items[name] = {"status": "no_date"}
            continue
        urls = resolve_target_urls(ini, pages_cfg)
        if not urls:
            items[name] = {"status": "no_target", "implemented_at": implemented_at.isoformat()}
            continue
        windows = initiative_windows(implemented_at, end, max_days)
        if windows is None:
            items[name] = {"status": "not_started", "implemented_at": implemented_at.isoformat(), "urls": urls}
            continue
        try:
            data = fetch_initiative(service, urls, windows)
        except HttpError as e:
            items[name] = {"status": "failed", "error": str(e)[:300], "implemented_at": implemented_at.isoformat(),
                           "urls": urls, "windows": windows}
            continue
        items[name] = {"status": "ok", "implemented_at": implemented_at.isoformat(), "urls": urls,
                       "windows": windows, **data}
        pre_start = date.fromisoformat(windows["pre"]["start"])
        earliest = pre_start if earliest is None or pre_start < earliest else earliest

    site_daily: dict[str, dict] = {}
    site_error = None
    if earliest is not None:
        try:
            rows = _query(service, {"startDate": earliest.isoformat(), "endDate": gsc_end, "dimensions": ["date"]})
            site_daily = {r["keys"][0]: _metrics(r) for r in rows}
        except HttpError as e:
            site_error = str(e)[:300]
    return {"version": INITIATIVES_VERSION, "gsc_end": gsc_end, "max_days": max_days,
            "site_daily": site_daily, "site_error": site_error, "items": items}


def fetch_for_period(service: Any, period: dict, initiatives_md: Path, pages_cfg: list[dict], max_days: int = 28) -> dict:
    from src_v2.analysis.initiatives import parse_initiatives_md

    if not Path(initiatives_md).exists():
        return {"version": INITIATIVES_VERSION, "gsc_end": period["cur_end"], "items": {}, "site_daily": {},
                "site_error": f"{initiatives_md} が無い"}
    return fetch_all(service, parse_initiatives_md(initiatives_md), pages_cfg, period["cur_end"], max_days)


def backfill(period_dir: Path, credentials_file: str = "credentials.json", force: bool = False,
             initiatives_md: Path = Path("docs/current-initiatives.md")) -> bool:
    """取得済みの normalized.json に施策の比較データが無い場合だけ取得して書き込む。書き込んだら True。"""
    from src_v2 import config_loader
    from src_v2.auth import get_search_console_service

    path = Path(period_dir) / "normalized.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if "report_week_start" not in bundle["period"]:
        return False
    if (bundle.get("initiatives_gsc") or {}).get("version") == INITIATIVES_VERSION and not force:
        return False
    max_days = config_loader.load_thresholds()["initiative_review"]["max_window_days"]
    service = get_search_console_service(credentials_file)
    bundle["initiatives_gsc"] = fetch_for_period(service, bundle["period"], initiatives_md, config_loader.load_pages(), max_days)
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="取得済みの週に、施策の実装前後の比較データを書き込む")
    parser.add_argument("--period-dir", required=True)
    parser.add_argument("--credentials-file", default="credentials.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if backfill(Path(args.period_dir), args.credentials_file, args.force):
        print("施策の比較データを書き込んだ")
    else:
        print("施策の比較データ: 書き込み済み（または旧方式の週）のため取り直さない")


if __name__ == "__main__":
    main()
