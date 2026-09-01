#!/usr/bin/env python3
"""TCD AIO 日次モニター。

config/aio_queries.yml に登録したクエリについて、SerpAPI経由で
Google AI Overview の有無・TCD引用状況・引用元ドメインを毎日取得し、

  - data/aio/daily/YYYY-MM-DD.json  … 生データ（AIO全文含む）
  - data/aio/looker/aio_daily.csv         … Looker Studio用（クエリ粒度）
  - data/aio/looker/aio_domains_daily.csv … Looker Studio用（引用ドメイン粒度）

として保存する。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

DEFAULT_CONFIG_PATH = "config/aio_queries.yml"
DEFAULT_DAILY_JSON_DIR = "data/aio/daily"
DEFAULT_LOOKER_DIR = "data/aio/looker"
AIO_DAILY_CSV_NAME = "aio_daily.csv"
AIO_DOMAINS_CSV_NAME = "aio_domains_daily.csv"
DEFAULT_LOG_PATH = "logs/aio_daily.log"

TCD_DOMAIN_DEFAULT = "tcd.jp"

DAILY_CSV_COLUMNS = [
    "date", "query", "aio_exists", "tcd_cited", "tcd_mentioned",
    "aio_text_hash", "aio_text_changed", "observation_type", "previous_hash_exists", "status",
]
DOMAINS_CSV_COLUMNS = ["date", "query", "cited_domain", "cited_domain_count"]


def load_env(env_path: str = ".env") -> None:
    """.env を読み込み os.environ にセットする（python-dotenv 不要の簡易実装）。
    既に環境変数が設定されている場合は上書きしない。"""
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _extract_domain(url: str) -> str:
    m = re.search(r'https?://([^/]+)', url)
    return m.group(1) if m else url


def _root_domain(hostname: str) -> str:
    """www. サブドメインをルートドメインへ正規化する。"""
    h = (hostname or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


_DOMAIN_LIKE_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)


def _looks_like_domain(s: str) -> bool:
    """'www.example.com' のようなホスト名らしい文字列か判定する（会社名の誤正規化を防ぐ）。"""
    return bool(_DOMAIN_LIKE_RE.match((s or "").strip()))


def is_google_domain(domain: str) -> bool:
    """Google検索・Google自身のURL（support.google.comなど）かどうかを判定する。"""
    d = (domain or "").lower()
    return d == "google" or "google." in d


def _flatten_text_block(block: dict) -> list[str]:
    parts = []
    snippet = block.get("snippet")
    if snippet:
        parts.append(snippet)
    for item in block.get("list", []) or []:
        parts.extend(_flatten_text_block(item))
    for sub in block.get("text_blocks", []) or []:
        parts.extend(_flatten_text_block(sub))
    return parts


def extract_aio_text(ai_ov: dict) -> str:
    """ai_overview.text が空の場合、text_blocks（段落・リスト）から本文を再構成する。"""
    text = (ai_ov.get("text") or "").strip()
    if text:
        return text
    parts: list[str] = []
    for block in ai_ov.get("text_blocks", []) or []:
        parts.extend(_flatten_text_block(block))
    return "\n".join(parts).strip()


def _normalize_key(s: str) -> str:
    return re.sub(r'\s+', '', (s or '')).lower()


def build_domain_alias_lookup(domain_aliases: dict | None) -> dict[str, str]:
    """config の domain_aliases から 正規化キー→正規ドメイン の逆引き辞書を作る。"""
    lookup: dict[str, str] = {}
    for canonical, aliases in (domain_aliases or {}).items():
        lookup[_normalize_key(canonical)] = canonical
        for alias in aliases:
            lookup[_normalize_key(alias)] = canonical
    return lookup


def normalize_domain(raw: str, alias_lookup: dict[str, str]) -> str:
    """引用元表記（ドメイン／社名／英語表記）を正規化後のドメインに寄せる。"""
    if not raw:
        return raw
    return alias_lookup.get(_normalize_key(raw), raw)


def _empty_result(query: str, observed_at: str, status: str, error: str | None) -> dict:
    return {
        "observed_at": observed_at,
        "query": query,
        "status": status,
        "aio_exists": None,
        "tcd_cited": False,
        "tcd_mentioned": False,
        "aio_text": None,
        "cited_urls": [],
        "cited_domains": [],
        "cited_titles": [],
        "competitor_domains": [],
        "error": error,
    }


def fetch_aio_for_query(
    query: str,
    observed_at: str,
    serpapi_cfg: dict,
    api_key: str,
    alias_lookup: dict[str, str],
    tcd_domain: str,
) -> dict:
    """SerpAPI で1クエリ分の Google AI Overview を観測する。"""
    from serpapi import GoogleSearch

    result = _empty_result(query, observed_at, "OK_NO_AIO", None)
    result["aio_exists"] = False

    try:
        params = {
            "engine": serpapi_cfg.get("engine", "google"),
            "q": query,
            "google_domain": serpapi_cfg.get("google_domain", "google.co.jp"),
            "hl": serpapi_cfg.get("hl", "ja"),
            "gl": serpapi_cfg.get("gl", "jp"),
            "location": serpapi_cfg.get("location", "Japan"),
            "api_key": api_key,
        }
        res = GoogleSearch(params).get_dict()
        ai_ov = res.get("ai_overview")

        # フォールバック: page_token がある場合は google_ai_overview エンジンで再取得
        if not ai_ov and res.get("ai_overview_page_token"):
            aio_res = GoogleSearch({
                "engine": "google_ai_overview",
                "page_token": res["ai_overview_page_token"],
                "api_key": api_key,
            }).get_dict()
            ai_ov = aio_res.get("ai_overview")

        if ai_ov:
            result["aio_exists"] = True
            result["status"] = "OK_WITH_AIO"
            aio_text = extract_aio_text(ai_ov)
            result["aio_text"] = aio_text

            cited_urls: list[str] = []
            cited_domains: list[str] = []
            cited_titles: list[str] = []

            for ref in ai_ov.get("references", []):
                if isinstance(ref, str):
                    url, src_name, title = ref, "", ""
                else:
                    url = ref.get("url", "")
                    src = ref.get("source", {})
                    src_name = (src.get("name", "") if isinstance(src, dict) else str(src))
                    title = ref.get("title", "")
                # ドメイン（正規化キー）は必ずURLのホスト名から求め、
                # 表示名（会社名など）と混同しない。www.はルートドメインへ正規化する。
                url_domain = _root_domain(_extract_domain(url)) if url else ""
                fallback = _root_domain(src_name) if _looks_like_domain(src_name) else src_name
                domain = normalize_domain(url_domain, alias_lookup) if url_domain else normalize_domain(fallback, alias_lookup)
                if url:
                    cited_urls.append(url)
                if domain:
                    cited_domains.append(domain)
                if title:
                    cited_titles.append(title)

            result["cited_urls"] = cited_urls
            result["cited_domains"] = cited_domains
            result["cited_titles"] = cited_titles

            tcd_needle = tcd_domain.lower()
            tcd_cited = any(tcd_needle in d.lower() for d in cited_domains) or \
                        any(tcd_needle in u.lower() for u in cited_urls)
            result["tcd_cited"] = tcd_cited
            result["tcd_mentioned"] = tcd_cited or (tcd_needle in aio_text.lower())
            result["competitor_domains"] = [
                d for d in cited_domains
                if tcd_needle not in d.lower() and not is_google_domain(d)
            ]

    except Exception as e:
        result["status"] = "FETCH_ERROR"
        result["error"] = str(e)

    return result


def save_daily_json(results: list[dict], daily_json_dir: Path, observed_at: str) -> Path:
    daily_json_dir.mkdir(parents=True, exist_ok=True)
    path = daily_json_dir / f"{observed_at}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_previous_hashes(csv_path: Path, before_date: str) -> dict[str, str]:
    """aio_daily.csv から、指定日より前の各クエリの最新 aio_text_hash を取得する。"""
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path, dtype=str)
    if df.empty:
        return {}
    df = df[df["date"] < before_date]
    if df.empty:
        return {}
    latest = df.sort_values("date").groupby("query").tail(1)
    return dict(zip(latest["query"], latest["aio_text_hash"].fillna("")))


def build_daily_rows(results: list[dict], previous_hashes: dict[str, str]) -> list[dict]:
    """クエリ粒度の Looker Studio 用 CSV 行を組み立てる。

    初回観測（そのクエリについて過去のハッシュが存在しない）は「本文変更」に
    カウントせず、observation_type="baseline" として扱う。
    """
    rows = []
    for r in results:
        aio_text = r.get("aio_text") or ""
        text_hash = hashlib.sha256(aio_text.encode("utf-8")).hexdigest()[:16] if aio_text else ""
        previous_hash_exists = r["query"] in previous_hashes
        prev_hash = previous_hashes.get(r["query"], "")

        if not previous_hash_exists:
            observation_type = "baseline"
            aio_text_changed = False
        elif text_hash == prev_hash:
            observation_type = "unchanged"
            aio_text_changed = False
        else:
            observation_type = "changed"
            aio_text_changed = True

        rows.append({
            "date": r["observed_at"],
            "query": r["query"],
            "aio_exists": r["aio_exists"],
            "tcd_cited": r["tcd_cited"],
            "tcd_mentioned": r["tcd_mentioned"],
            "aio_text_hash": text_hash,
            "aio_text_changed": aio_text_changed,
            "observation_type": observation_type,
            "previous_hash_exists": previous_hash_exists,
            "status": r["status"],
        })
    return rows


def build_domain_rows(results: list[dict]) -> list[dict]:
    """引用ドメイン粒度（1行1ドメイン）の Looker Studio 用 CSV 行を組み立てる。
    Google検索・Google自身のドメインは競合ドメインではないため除外する。"""
    rows = []
    for r in results:
        counts: dict[str, int] = {}
        for d in r.get("cited_domains", []):
            if is_google_domain(d):
                continue
            counts[d] = counts.get(d, 0) + 1
        for domain, count in counts.items():
            rows.append({
                "date": r["observed_at"],
                "query": r["query"],
                "cited_domain": domain,
                "cited_domain_count": count,
            })
    return rows


def write_csv_rows(rows: list[dict], csv_path: Path, key_cols: list[str], column_order: list[str]) -> None:
    """行データを CSV に upsert する（同一キーの既存行は置き換え）。

    将来 Google Sheets 連携に切り替える場合は、この関数と同じ
    (rows, key_cols, column_order) インターフェースで
    スプレッドシート書き込み版を用意すればよい。
    """
    new_df = pd.DataFrame(rows, columns=column_order)
    if csv_path.exists():
        old_df = pd.read_csv(csv_path, dtype=str)
        old_keys = old_df[key_cols].astype(str).apply(tuple, axis=1)
        new_keys = new_df[key_cols].astype(str).apply(tuple, axis=1)
        old_df = old_df[~old_keys.isin(set(new_keys))]
        combined = pd.concat([old_df, new_df], ignore_index=True)
    else:
        combined = new_df
    combined = combined.sort_values(key_cols).reset_index(drop=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(csv_path, index=False)


def update_looker_csvs(results: list[dict], looker_dir: Path, observed_at: str) -> tuple[Path, Path]:
    aio_daily_csv = looker_dir / AIO_DAILY_CSV_NAME
    aio_domains_csv = looker_dir / AIO_DOMAINS_CSV_NAME

    previous_hashes = get_previous_hashes(aio_daily_csv, observed_at)
    daily_rows = build_daily_rows(results, previous_hashes)
    domain_rows = build_domain_rows(results)

    write_csv_rows(daily_rows, aio_daily_csv, key_cols=["date", "query"], column_order=DAILY_CSV_COLUMNS)
    write_csv_rows(domain_rows, aio_domains_csv, key_cols=["date", "query", "cited_domain"], column_order=DOMAINS_CSV_COLUMNS)

    return aio_daily_csv, aio_domains_csv


def build_run_summary(results: list[dict], run_at: datetime) -> str:
    n_total = len(results)
    n_aio = sum(1 for r in results if r.get("aio_exists"))
    n_cited = sum(1 for r in results if r.get("tcd_cited"))
    n_error = sum(1 for r in results if r.get("status") == "FETCH_ERROR")

    lines = [
        run_at.strftime("%Y-%m-%d %H:%M"),
        f"{n_total} queries checked",
        f"AIO: {n_aio}",
        f"TCD cited: {n_cited}",
    ]
    if n_error:
        lines.append(f"Errors: {n_error}")
    lines.append("CSV updated.")
    return "\n".join(lines)


def append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="TCD AIO 日次モニター")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="クエリ設定ファイル")
    parser.add_argument("--env-file", default=".env", help=".env ファイルパス")
    parser.add_argument("--daily-json-dir", default=DEFAULT_DAILY_JSON_DIR)
    parser.add_argument("--looker-dir", default=DEFAULT_LOOKER_DIR)
    parser.add_argument("--date", default=None, help="観測日を上書き（テスト用、YYYY-MM-DD）")
    parser.add_argument("--log-file", default=DEFAULT_LOG_PATH, help="実行サマリーの追記先ログファイル")
    args = parser.parse_args()

    load_env(args.env_file)
    config = load_config(args.config)

    queries = config.get("queries", [])
    if not queries:
        print(f"{args.config} にクエリが登録されていません。", file=sys.stderr)
        sys.exit(1)

    serpapi_cfg = config.get("serpapi", {})
    tcd_domain = config.get("tcd_domain", TCD_DOMAIN_DEFAULT)
    alias_lookup = build_domain_alias_lookup(config.get("domain_aliases"))
    api_key = os.environ.get("SERPAPI_API_KEY")

    observed_at = args.date or date.today().strftime("%Y-%m-%d")

    if not api_key:
        print("SERPAPI_API_KEY が未設定です。.env を確認してください。", file=sys.stderr)
        results = [_empty_result(q, observed_at, "API_KEY_MISSING", "SERPAPI_API_KEY 未設定") for q in queries]
    else:
        results = []
        for query in queries:
            print(f"取得中: 「{query}」...")
            results.append(fetch_aio_for_query(query, observed_at, serpapi_cfg, api_key, alias_lookup, tcd_domain))

    json_path = save_daily_json(results, Path(args.daily_json_dir), observed_at)
    print(f"日次JSON保存: {json_path}")

    aio_daily_csv, aio_domains_csv = update_looker_csvs(results, Path(args.looker_dir), observed_at)
    print(f"Looker CSV更新: {aio_daily_csv}")
    print(f"Looker CSV更新: {aio_domains_csv}")

    summary = build_run_summary(results, datetime.now())
    print(summary)
    append_log(Path(args.log_file), summary)


if __name__ == "__main__":
    main()
