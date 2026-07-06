#!/usr/bin/env python3
"""
TCD SEO/AIO Monthly Report Generator
Google Search Console API (OAuth2) を使用して TCD サイトの月次 SEO/AIO レポートを自動生成します

使い方:
  python generate_report.py
  python generate_report.py --credentials-file /path/to/credentials.json
  python generate_report.py --config config/aio_monitor.yml --output-dir reports
"""
from __future__ import annotations

import os
import sys

# .env ファイルのサポート（python-dotenv がある場合のみ読み込む）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import argparse
import pickle
import base64
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import re

import yaml
import pandas as pd
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ────────────────────────────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
SC_MAX_ROWS = 25_000    # Search Console API の1リクエスト上限

# 掲載順位別の期待CTR（Backlinko 2023 調査ベース）
EXPECTED_CTR_BY_POS: dict[int, float] = {
    1: 0.284, 2: 0.152, 3: 0.107, 4: 0.079, 5: 0.060,
    6: 0.047, 7: 0.038, 8: 0.031, 9: 0.026, 10: 0.022,
}



# ────────────────────────────────────────────────────────────────────
# 設定ファイル読み込み
# ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config/aio_monitor.yml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ────────────────────────────────────────────────────────────────────
# 認証
# ────────────────────────────────────────────────────────────────────

def _get_oauth_credentials(credentials_file: str = "credentials.json") -> Any:
    """OAuth2 Credentials を返す。token.pickle にキャッシュ。スコープ変更時は自動再認証。"""
    creds = None
    token_path = Path("token.pickle")

    if token_path.exists():
        creds = pickle.loads(token_path.read_bytes())
        # スコープが増えていたら再認証
        if creds and creds.scopes is not None:
            if not set(SCOPES).issubset(set(creds.scopes)):
                print("  スコープが変更されたため再認証します（ブラウザが開きます）...")
                creds = None
                token_path.unlink()

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            oauth_file = Path(credentials_file)
            if not oauth_file.exists():
                print(
                    f"\nエラー: {credentials_file} が見つかりません。\n"
                    "Google Cloud Console で OAuth2 クライアント ID（デスクトップアプリ）を作成し、\n"
                    "JSON をダウンロードして credentials.json としてプロジェクトルートに配置してください。\n"
                    "詳細は setup.md を参照。",
                    file=sys.stderr,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_bytes(pickle.dumps(creds))

    return creds


def get_service(credentials_file: str = "credentials.json") -> Any:
    """認証済み Search Console API サービスを返す。"""
    creds = _get_oauth_credentials(credentials_file)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def get_gmail_service(credentials_file: str = "credentials.json") -> Any:
    """認証済み Gmail API サービスを返す。"""
    creds = _get_oauth_credentials(credentials_file)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_html_email(gmail_service: Any, to: str, subject: str, html_content: str) -> None:
    """Gmail API を使って HTML メールを送信する。"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = to
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ────────────────────────────────────────────────────────────────────
# 期間計算
# ────────────────────────────────────────────────────────────────────

def get_periods(period_type: str = "monthly") -> tuple[str, str, str, str]:
    """実行日と period_type に応じた (cur_start, cur_end, prv_start, prv_end) を YYYY-MM-DD で返す。

    auto（毎週月曜9:00の運用コマンド）:
      月の 1〜7 日かつ月曜  →  monthly（月替わり後最初の月曜）
      それ以外の月曜         →  weekly（通常週）

    weekly:
      月曜実行: 先週月曜〜先週日曜  vs  先々週月曜〜先々週日曜
      月曜以外（手動テスト）: データラグ3日方式

    monthly:
      前月 1 日〜前月末日  vs  前々月 1 日〜前々月末日
    """
    today = datetime.today()
    fmt = lambda d: d.strftime("%Y-%m-%d")

    if period_type == "auto":
        period_type = "monthly" if today.weekday() == 0 and today.day <= 7 else "weekly"

    if period_type == "weekly":
        if today.weekday() == 0:
            cur_end   = today - timedelta(days=1)   # 日曜
            cur_start = cur_end - timedelta(days=6) # 月曜
        else:
            cur_end   = today - timedelta(days=3)   # データラグ（手動テスト用）
            cur_start = cur_end - timedelta(days=6)
        prv_end   = cur_start - timedelta(days=1)
        prv_start = prv_end - timedelta(days=6)
    else:  # monthly
        cur_end   = today.replace(day=1) - timedelta(days=1)  # 前月末日
        cur_start = cur_end.replace(day=1)                    # 前月1日
        prv_end   = cur_start - timedelta(days=1)             # 前々月末日
        prv_start = prv_end.replace(day=1)                    # 前々月1日

    return fmt(cur_start), fmt(cur_end), fmt(prv_start), fmt(prv_end)


# ────────────────────────────────────────────────────────────────────
# API データ取得
# ────────────────────────────────────────────────────────────────────

def _fetch_rows(
    service: Any,
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    dimension_filter: dict | None = None,
    row_limit: int = 5000,
) -> list[dict]:
    """ページネーション込みで Search Analytics データを取得する。"""
    rows: list[dict] = []
    start_row = 0

    while len(rows) < row_limit:
        body: dict = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions,
            "rowLimit": min(SC_MAX_ROWS, row_limit - len(rows)),
            "startRow": start_row,
        }
        if dimension_filter:
            body["dimensionFilterGroups"] = [{"filters": [dimension_filter]}]

        try:
            resp = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        except HttpError as e:
            print(f"  API エラー: {e}", file=sys.stderr)
            break

        batch = resp.get("rows", [])
        if not batch:
            break

        rows.extend(batch)
        start_row += len(batch)
        if len(batch) < SC_MAX_ROWS:
            break

    return rows


def _to_df(rows: list[dict], dims: list[str]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=dims + ["clicks", "impressions", "ctr", "position"])
    records = [
        {
            **{d: row["keys"][i] for i, d in enumerate(dims)},
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0.0)),
            "position": float(row.get("position", 0.0)),
        }
        for row in rows
    ]
    return pd.DataFrame(records)


def fetch_queries(svc, site, s, e, n=5000) -> pd.DataFrame:
    return _to_df(_fetch_rows(svc, site, s, e, ["query"], row_limit=n), ["query"])

def fetch_pages(svc, site, s, e, n=5000) -> pd.DataFrame:
    return _to_df(_fetch_rows(svc, site, s, e, ["page"], row_limit=n), ["page"])

def fetch_devices(svc, site, s, e) -> pd.DataFrame:
    return _to_df(_fetch_rows(svc, site, s, e, ["device"]), ["device"])


# ────────────────────────────────────────────────────────────────────
# データ処理・集計
# ────────────────────────────────────────────────────────────────────

def calc_totals(page_df: pd.DataFrame) -> dict:
    """ページ DataFrame からサイト全体の集計値を返す。"""
    if page_df.empty:
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    c = int(page_df["clicks"].sum())
    im = int(page_df["impressions"].sum())
    pos = float(
        (page_df["position"] * page_df["impressions"]).sum() / im
    ) if im else 0.0
    return {"clicks": c, "impressions": im, "ctr": c / im if im else 0.0, "position": pos}


def pct_chg(cur: float, prv: float) -> float | None:
    if prv == 0:
        return None
    return (cur - prv) / prv * 100


def _aio_score(clicks: float, impressions: float, position: float) -> float:
    """AIO 暴露スコア: 0=期待通りのクリック獲得 / 1=クリックゼロ。
    掲載順位に対するCTR期待値との乖離で AI Overview によるCTR損失を推定。"""
    if not impressions or not position:
        return 0.0
    pos_int = max(1, min(10, round(position)))
    expected_ctr = EXPECTED_CTR_BY_POS.get(pos_int, 0.02)
    expected_clicks = impressions * expected_ctr
    if not expected_clicks:
        return 0.0
    return max(0.0, min(1.0, 1.0 - clicks / expected_clicks))


def _normalize_query(s: str) -> str:
    """半角・全角スペースをすべて除去して小文字化する（スペースバリアント照合用）。"""
    return re.sub(r'[\s　]+', '', s).lower()


# ────────────────────────────────────────────────────────────────────
# レポートスクリーニングルール定数
# ────────────────────────────────────────────────────────────────────

MIN_IMP_FINDING = 30    # 今月の発見・仮説に使う最低Imp閾値
MIN_IMP_AIO     = 50    # CTR確認候補の最低Imp閾値
MAX_POS_AIO     = 15    # CTR確認候補の最大順位
MAX_CTR_AIO     = 0.01  # CTR確認候補の最大CTR（1%）
RANK_CHG_MIN    = 3.0   # 順位変化として記録する最小変化量（位）

_EXCLUDE_PATTERNS = [
    "アシックス", "asics", "アイラブニューヨーク", "i love ny",
    "mcc食品", "mcc", "ロゴ", "マーク",
]
_WHITELIST_TERMS = [
    "ブランディング", "ブランド", "インナーブランディング",
    "理念浸透", "組織ブランディング", "ネーミング",
    "プロダクトブランディング", "tcd", "株式会社tcd",
]


def is_business_relevant(query: str) -> bool:
    """事業関連性フィルタ: 除外パターンに一致→False、ホワイトリストに一致→True。"""
    nq = _normalize_query(query)
    for pat in _EXCLUDE_PATTERNS:
        if _normalize_query(pat) in nq:
            return False
    for term in _WHITELIST_TERMS:
        if _normalize_query(term) in nq:
            return True
    return False


def load_watching_csv(csv_path: str = "Watching.csv") -> dict[str, list[dict]]:
    """Watching.csv から監視ページリストを読み込む。
    「セクション名：タイトル」行をセクション区切りとして解析する。"""
    import csv as _csv
    sections: dict[str, list[dict]] = {}
    current: str | None = None
    try:
        with open(csv_path, encoding="utf-8") as f:
            for row in _csv.reader(f):
                if not row or not row[0].strip():
                    continue
                col0 = row[0].strip()
                if "：タイトル" in col0:
                    current = col0.split("：")[0].strip()
                    sections[current] = []
                elif current and len(row) >= 2 and row[1].strip().startswith("http"):
                    title = col0
                    sections[current].append({
                        "title": title,
                        "name":  title,
                        "url":   row[1].strip(),
                    })
    except FileNotFoundError:
        print(f"警告: {csv_path} が見つかりません。ページ監視リストが空になります。", file=sys.stderr)
    return sections


def _extract_domain(url: str) -> str:
    m = re.search(r'https?://([^/]+)', url)
    return m.group(1) if m else url


def build_domain_alias_lookup(domain_aliases: dict | None) -> dict[str, str]:
    """config の domain_aliases から 正規化キー→正規ドメイン の逆引き辞書を作る。"""
    lookup: dict[str, str] = {}
    for canonical, aliases in (domain_aliases or {}).items():
        lookup[_normalize_query(canonical)] = canonical
        for alias in aliases:
            lookup[_normalize_query(alias)] = canonical
    return lookup


def normalize_domain(raw: str, alias_lookup: dict[str, str]) -> str:
    """引用元表記（ドメイン／社名／英語表記）を正規化後のドメインに寄せる。
    alias_lookup に一致がなければ元の表記のまま返す。"""
    if not raw:
        return raw
    return alias_lookup.get(_normalize_query(raw), raw)


def fetch_google_aio(
    queries: list[str],
    aio_cfg: dict | None = None,
    output_dir: str = "reports/google_aio",
    force: bool = False,
    domain_aliases: dict | None = None,
) -> list[dict]:
    """SerpAPI で Google 検索し、AI Overview の有無・TCD引用・引用URLを観測する。
    同日キャッシュが存在する場合はそれを返す（force=True で強制再取得）。"""
    import json
    from datetime import date

    api_key = os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("  SERPAPI_API_KEY が未設定のため AIO 観測をスキップします。", file=sys.stderr)
        return [{"query": q, "observed_at": date.today().strftime("%Y-%m-%d"),
                 "status": "API_KEY_MISSING", "aio_exists": None,
                 "tcd_mentioned": False, "tcd_cited": False,
                 "cited_urls": [], "cited_domains": [], "competitor_domains": [],
                 "error": "SERPAPI_API_KEY 未設定"} for q in queries]

    cfg = aio_cfg or {}
    alias_lookup = build_domain_alias_lookup(domain_aliases)
    today_str  = date.today().strftime("%Y-%m-%d")
    out_dir    = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / f"{today_str}.json"

    if cache_path.exists() and not force:
        print(f"  AIOキャッシュ使用: {cache_path}")
        return json.loads(cache_path.read_text(encoding="utf-8"))

    try:
        from serpapi import GoogleSearch
    except ImportError:
        print("  google-search-results 未インストール。pip install google-search-results", file=sys.stderr)
        return []

    _TCD_PATTERNS = ["tcd.jp", "株式会社tcd", " tcd ", "（tcd）"]
    results: list[dict] = []

    for query in queries:
        print(f"  SerpAPI AIO確認中: 「{query}」...")

        aio_exists        = False
        aio_text          = None
        cited_urls:   list[str] = []
        cited_domains:list[str] = []
        cited_titles: list[str] = []
        tcd_mentioned     = False
        tcd_cited         = False
        tcd_cited_urls:list[str] = []
        competitor_domains:list[str] = []
        status            = "OK_NO_AIO"
        error             = None

        try:
            params = {
                "engine":        cfg.get("engine", "google"),
                "q":             query,
                "google_domain": cfg.get("google_domain", "google.co.jp"),
                "hl":            cfg.get("hl", "ja"),
                "gl":            cfg.get("gl", "jp"),
                "location":      cfg.get("location", "Japan"),
                "api_key":       api_key,
            }
            res = GoogleSearch(params).get_dict()

            ai_ov = res.get("ai_overview")

            # フォールバック: page_token がある場合は google_ai_overview エンジンで再取得
            if not ai_ov and res.get("ai_overview_page_token"):
                aio_res = GoogleSearch({
                    "engine":   "google_ai_overview",
                    "page_token": res["ai_overview_page_token"],
                    "api_key":  api_key,
                }).get_dict()
                ai_ov = aio_res.get("ai_overview")

            if ai_ov:
                aio_exists = True
                status     = "OK_WITH_AIO"
                aio_text   = ai_ov.get("text", "")

                for ref in ai_ov.get("references", []):
                    if isinstance(ref, str):
                        url, domain, title = ref, _extract_domain(ref), ""
                    else:
                        url    = ref.get("url", "")
                        src    = ref.get("source", {})
                        domain = (src.get("name", "") if isinstance(src, dict) else str(src)) or _extract_domain(url)
                        title  = ref.get("title", "")
                    domain = normalize_domain(domain, alias_lookup)
                    if url:    cited_urls.append(url)
                    if domain: cited_domains.append(domain)
                    if title:  cited_titles.append(title)

                    chk = (url + " " + domain + " " + title).lower()
                    if any(p.lower() in chk for p in _TCD_PATTERNS):
                        tcd_cited = True
                        tcd_cited_urls.append(url)

                check_text = (aio_text or "").lower()
                tcd_mentioned = any(p.lower() in check_text for p in _TCD_PATTERNS) or tcd_cited
                competitor_domains = [d for d in cited_domains
                                      if not any(p.lower() in d.lower() for p in _TCD_PATTERNS)][:5]

        except Exception as e:
            error  = str(e)
            status = "FETCH_ERROR"
            print(f"  SerpAPI エラー ({query}): {e}", file=sys.stderr)

        results.append({
            "query":              query,
            "observed_at":        today_str,
            "status":             status,
            "aio_exists":         aio_exists,
            "aio_text":           aio_text,
            "cited_urls":         cited_urls,
            "cited_domains":      cited_domains,
            "cited_titles":       cited_titles,
            "tcd_mentioned":      tcd_mentioned,
            "tcd_cited":          tcd_cited,
            "tcd_cited_urls":     tcd_cited_urls,
            "competitor_domains": competitor_domains,
            "error":              error,
        })

    cache_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  AIO観測結果保存: {cache_path}")
    return results


# ────────────────────────────────────────────────────────────────────
# aio_daily_monitor.py 連携（日次AIO観測データの読み込み・集計）
# ────────────────────────────────────────────────────────────────────

AIO_DAILY_CSV_PATH   = "data/aio/looker/aio_daily.csv"
AIO_DOMAINS_CSV_PATH = "data/aio/looker/aio_domains_daily.csv"

_AIO_DAILY_COLUMNS = [
    "date", "query", "aio_exists", "tcd_cited", "tcd_mentioned",
    "aio_text_hash", "aio_text_changed", "status",
]
_AIO_DOMAINS_COLUMNS = ["date", "query", "cited_domain", "cited_domain_count"]
_AIO_DAILY_BOOL_COLUMNS = ["aio_exists", "tcd_cited", "tcd_mentioned", "aio_text_changed"]


def load_aio_daily_csv(path: str = AIO_DAILY_CSV_PATH) -> pd.DataFrame:
    """aio_daily_monitor.py が出力する aio_daily.csv を読み込む。
    ファイル不在・読み込み失敗・列不足のいずれでも例外を出さず、空/補完済みDataFrameを返す。"""
    p = Path(path)
    if not p.exists():
        print(f"  AIO日次データなし: {path}（aio_daily_monitor.py が未実行のためスキップ）", file=sys.stderr)
        return pd.DataFrame(columns=_AIO_DAILY_COLUMNS)

    try:
        df = pd.read_csv(p, dtype=str)
    except Exception as e:
        print(f"  警告: AIO日次CSV読み込み失敗: {path} ({e})", file=sys.stderr)
        return pd.DataFrame(columns=_AIO_DAILY_COLUMNS)

    missing = [c for c in _AIO_DAILY_COLUMNS if c not in df.columns]
    if missing:
        print(f"  警告: {path} に必要な列がありません（{missing}）。欠損値として扱います。", file=sys.stderr)
        for c in missing:
            df[c] = None

    for col in _AIO_DAILY_BOOL_COLUMNS:
        df[col] = df[col].map(lambda v: str(v).strip().lower() == "true")

    print(f"  AIO日次CSV読み込み成功: {path}（{len(df)}行）")
    return df


def load_aio_domains_daily_csv(path: str = AIO_DOMAINS_CSV_PATH) -> pd.DataFrame:
    """aio_daily_monitor.py が出力する aio_domains_daily.csv を読み込む。
    ファイル不在・読み込み失敗・列不足のいずれでも例外を出さず、空/補完済みDataFrameを返す。"""
    p = Path(path)
    if not p.exists():
        print(f"  AIO引用ドメインデータなし: {path}（aio_daily_monitor.py が未実行のためスキップ）", file=sys.stderr)
        return pd.DataFrame(columns=_AIO_DOMAINS_COLUMNS)

    try:
        df = pd.read_csv(p, dtype=str)
    except Exception as e:
        print(f"  警告: AIO引用ドメインCSV読み込み失敗: {path} ({e})", file=sys.stderr)
        return pd.DataFrame(columns=_AIO_DOMAINS_COLUMNS)

    missing = [c for c in _AIO_DOMAINS_COLUMNS if c not in df.columns]
    if missing:
        print(f"  警告: {path} に必要な列がありません（{missing}）。欠損値として扱います。", file=sys.stderr)
        for c in missing:
            df[c] = None

    df["cited_domain_count"] = pd.to_numeric(df["cited_domain_count"], errors="coerce").fillna(0).astype(int)

    print(f"  AIO引用ドメインCSV読み込み成功: {path}（{len(df)}行）")
    return df


def aggregate_aio_daily(df: pd.DataFrame, start: str, end: str) -> dict:
    """aio_daily.csv を、週次/月次レポートと同じ対象期間で集計する。"""
    empty = {
        "aio_query_count": 0, "aio_exists_count": 0, "aio_exists_rate": None,
        "tcd_cited_count": 0, "tcd_cited_rate": None, "tcd_mentioned_count": 0,
        "aio_text_changed_count": 0, "error_count": 0, "query_level": [],
    }
    if df.empty or "date" not in df.columns:
        return empty

    period = df[(df["date"] >= start) & (df["date"] <= end)]
    if period.empty:
        return empty

    n_rows               = len(period)
    aio_exists_count      = int(period["aio_exists"].sum())
    tcd_cited_count       = int(period["tcd_cited"].sum())
    tcd_mentioned_count   = int(period["tcd_mentioned"].sum())
    aio_text_changed_count = int(period["aio_text_changed"].sum())
    error_count           = int((period["status"] == "FETCH_ERROR").sum())

    query_level = []
    for query, g in period.groupby("query"):
        n_days   = len(g)
        aio_days = int(g["aio_exists"].sum())
        query_level.append({
            "query":                 query,
            "aio_exists_days":       aio_days,
            "aio_exists_rate":       aio_days / n_days if n_days else None,
            "tcd_cited_days":        int(g["tcd_cited"].sum()),
            "tcd_mentioned_days":    int(g["tcd_mentioned"].sum()),
            "latest_status":         g.sort_values("date").iloc[-1]["status"],
            "aio_text_changed_count": int(g["aio_text_changed"].sum()),
        })
    query_level.sort(key=lambda r: -(r["aio_exists_rate"] or 0))

    return {
        "aio_query_count":        period["query"].nunique(),
        "aio_exists_count":       aio_exists_count,
        "aio_exists_rate":        aio_exists_count / n_rows if n_rows else None,
        "tcd_cited_count":        tcd_cited_count,
        "tcd_cited_rate":         tcd_cited_count / n_rows if n_rows else None,
        "tcd_mentioned_count":    tcd_mentioned_count,
        "aio_text_changed_count": aio_text_changed_count,
        "error_count":            error_count,
        "query_level":            query_level,
    }


def aggregate_aio_domain_trend(df: pd.DataFrame, cur_start: str, cur_end: str, prv_start: str, prv_end: str) -> dict:
    """aio_domains_daily.csv から、引用ドメインランキング・新規/消失ドメインを集計する。
    first_seen / last_seen はCSV全履歴から算出する（今期・前期に限定しない）。"""
    empty = {"top_domains": [], "new_domains": [], "lost_domains": [], "lost_rows": []}
    if df.empty or "date" not in df.columns:
        return empty

    cur = df[(df["date"] >= cur_start) & (df["date"] <= cur_end)]
    prv = df[(df["date"] >= prv_start) & (df["date"] <= prv_end)]

    cur_domains = set(cur["cited_domain"].unique())
    prv_domains = set(prv["cited_domain"].unique())

    def _first_last(domain: str) -> tuple[str, str]:
        hist = df[df["cited_domain"] == domain]["date"]
        return hist.min(), hist.max()

    top_domains = []
    for domain, g in cur.groupby("cited_domain"):
        first_seen, last_seen = _first_last(domain)
        top_domains.append({
            "domain":          domain,
            "cited_count":     int(g["cited_domain_count"].sum()),
            "related_queries": sorted(g["query"].unique().tolist()),
            "first_seen":      first_seen,
            "last_seen":       last_seen,
            "status":          "new" if domain not in prv_domains else "",
        })
    top_domains.sort(key=lambda r: -r["cited_count"])

    lost_domains = sorted(prv_domains - cur_domains)
    lost_rows = []
    for domain in lost_domains:
        g = prv[prv["cited_domain"] == domain]
        first_seen, last_seen = _first_last(domain)
        lost_rows.append({
            "domain":          domain,
            "cited_count":     int(g["cited_domain_count"].sum()),
            "related_queries": sorted(g["query"].unique().tolist()),
            "first_seen":      first_seen,
            "last_seen":       last_seen,
            "status":          "lost",
        })

    return {
        "top_domains":  top_domains,
        "new_domains":  sorted(cur_domains - prv_domains),
        "lost_domains": lost_domains,
        "lost_rows":    lost_rows,
    }


def aggregate_aio_text_changes(df: pd.DataFrame, start: str, end: str, limit: int = 10) -> dict:
    """対象期間中にAIO本文が変化したクエリを集計する（本文そのものは含めない）。"""
    if df.empty or "date" not in df.columns:
        return {"changed_count": 0, "changed_queries": []}

    period  = df[(df["date"] >= start) & (df["date"] <= end)]
    changed = period[period["aio_text_changed"]].sort_values("date", ascending=False)
    return {
        "changed_count":   len(changed),
        "changed_queries": changed[["date", "query"]].to_dict("records")[:limit],
    }


def kw_compare(cdf: pd.DataFrame, pdf: pd.DataFrame, keywords: list[str]) -> list[dict]:
    """スペースバリアント（半角・全角・なし）を正規化して照合・集計する。"""
    def _agg(df: pd.DataFrame, norm_kw: str) -> dict:
        empty = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        if df.empty:
            return empty
        matched = df[df["query"].apply(_normalize_query) == norm_kw]
        if matched.empty:
            return empty
        clicks = int(matched["clicks"].sum())
        impressions = int(matched["impressions"].sum())
        ctr = clicks / impressions if impressions else 0.0
        position = float(
            (matched["position"] * matched["impressions"]).sum() / impressions
        ) if impressions else 0.0
        return {"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}

    out = []
    for kw in keywords:
        norm = _normalize_query(kw)
        cd = _agg(cdf, norm)
        pd_ = _agg(pdf, norm)
        out.append({
            "query": kw,
            "cur": cd, "prv": pd_,
            "click_chg": pct_chg(float(cd["clicks"]), float(pd_["clicks"])),
            "pos_chg": float(cd["position"]) - float(pd_["position"]),
            "ctr_chg": float(cd["ctr"]) - float(pd_["ctr"]),
        })
    return out


def _lookup_page(df: pd.DataFrame, url: str) -> dict:
    """末尾スラッシュ有無の両バリアントで照合し、ヒット行を集計して返す。"""
    empty = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    if df.empty:
        return empty
    variants = {url, url.rstrip("/"), url.rstrip("/") + "/"}
    matched = df[df["page"].isin(variants)]
    if matched.empty:
        return empty
    clicks = int(matched["clicks"].sum())
    impressions = int(matched["impressions"].sum())
    ctr = clicks / impressions if impressions else 0.0
    position = float(
        (matched["position"] * matched["impressions"]).sum() / impressions
    ) if impressions else 0.0
    return {"clicks": clicks, "impressions": impressions, "ctr": ctr, "position": position}


def page_compare(cdf: pd.DataFrame, pdf: pd.DataFrame, cfgs: list[dict], site_url: str) -> list[dict]:
    base = site_url.rstrip("/")
    out = []
    for cfg in cfgs:
        path = cfg.get("url", "")
        name = cfg.get("name", path)
        full_url = (base + path) if path.startswith("/") else path
        cd = _lookup_page(cdf, full_url)
        pd_ = _lookup_page(pdf, full_url)
        score = _aio_score(float(cd["clicks"]), float(cd["impressions"]), float(cd["position"]))
        out.append({
            "name": name, "title": cfg.get("title", name), "url": full_url,
            "cur": cd, "prv": pd_,
            "click_chg": pct_chg(float(cd["clicks"]), float(pd_["clicks"])),
            "pos_chg": float(cd["position"]) - float(pd_["position"]),
            "ctr_chg": float(cd["ctr"]) - float(pd_["ctr"]),
            "aio_score": score,
        })
    return out


def compute_index_monitoring(entries: list[dict], cur_df: pd.DataFrame, prv_df: pd.DataFrame, site_url: str) -> list[dict]:
    """新規・更新ページがSearch Console上に出始めたかを追跡する。
    注: Search Analytics APIにはインデックス有無の情報がないため、
    「indexed_no_impression」（クロール済みだが表示回数ゼロ）はURL Inspection API連携が必要で未対応。
    現状はImp/Clickの有無から not_seen / impression_started / ranking_started の3状態を判定する。"""
    base = site_url.rstrip("/")
    out = []
    for e in entries:
        path  = e.get("url", "")
        title = e.get("title", path)
        pub_date = e.get("published_or_updated_date", "")
        full_url = (base + path) if path.startswith("/") else path

        cur = _lookup_page(cur_df, full_url)
        prv = _lookup_page(prv_df, full_url)
        ever_seen = cur["impressions"] > 0 or prv["impressions"] > 0

        if not ever_seen:
            status = "not_seen"
            first_seen = None
        elif cur["clicks"] > 0:
            status = "ranking_started"
            first_seen = pub_date or None
        else:
            status = "impression_started"
            first_seen = pub_date or None

        out.append({
            "page_title":                    title,
            "url":                           full_url,
            "published_or_updated_date":     pub_date,
            "first_seen_in_search_console":  first_seen,
            "impressions":                   cur["impressions"],
            "clicks":                        cur["clicks"],
            "average_position":              cur["position"],
            "status":                        status,
        })
    return out


def top_queries_cmp(cdf: pd.DataFrame, pdf: pd.DataFrame, n: int = 20) -> list[dict]:
    if cdf.empty:
        return []
    pm = pdf.set_index("query").to_dict("index") if not pdf.empty else {}
    return [
        {
            "query": r.query,
            "cur": {"clicks": int(r.clicks), "impressions": int(r.impressions), "ctr": float(r.ctr), "position": float(r.position)},
            "prv": {"clicks": int(pm.get(r.query, {}).get("clicks", 0)),
                    "impressions": int(pm.get(r.query, {}).get("impressions", 0)),
                    "ctr": float(pm.get(r.query, {}).get("ctr", 0)),
                    "position": float(pm.get(r.query, {}).get("position", 0))},
            "click_chg": pct_chg(int(r.clicks), int(pm.get(r.query, {}).get("clicks", 0))),
        }
        for _, r in cdf.nlargest(n, "clicks").iterrows()
    ]


def top_pages_cmp(cdf: pd.DataFrame, pdf: pd.DataFrame, n: int = 20) -> list[dict]:
    if cdf.empty:
        return []
    pm = pdf.set_index("page").to_dict("index") if not pdf.empty else {}
    return [
        {
            "page": r.page,
            "cur": {"clicks": int(r.clicks), "impressions": int(r.impressions), "ctr": float(r.ctr), "position": float(r.position)},
            "prv": {"clicks": int(pm.get(r.page, {}).get("clicks", 0)),
                    "impressions": int(pm.get(r.page, {}).get("impressions", 0)),
                    "ctr": float(pm.get(r.page, {}).get("ctr", 0)),
                    "position": float(pm.get(r.page, {}).get("position", 0))},
            "click_chg": pct_chg(int(r.clicks), int(pm.get(r.page, {}).get("clicks", 0))),
        }
        for _, r in cdf.nlargest(n, "clicks").iterrows()
    ]


def detect_aio_anomalies(qdf: pd.DataFrame, min_impr: int = 100, top_n: int = 10) -> list[dict]:
    """CTR が掲載順位に対する期待値を大きく下回るクエリを検出する。"""
    if qdf.empty:
        return []
    df = qdf[qdf["impressions"] >= min_impr].copy()
    df["aio"] = df.apply(
        lambda r: _aio_score(r["clicks"], r["impressions"], r["position"]), axis=1
    )
    top = df[df["aio"] > 0.4].nlargest(top_n, "aio")
    return top[["query", "impressions", "clicks", "ctr", "position", "aio"]].to_dict("records")


def aio_competitor_ranking(gaio: list[dict]) -> tuple[list[dict], int]:
    """AIOに引用された企業をAIシェア順にランキング化する（TCD含む）。
    Returns (ranking_list, total_aio_count)
    """
    total_aio = sum(1 for r in gaio if r.get("aio_exists"))
    if total_aio == 0:
        return [], 0

    counts: dict[str, int] = {}
    kw_map: dict[str, list[str]] = {}
    tcd_count = 0
    tcd_kws: list[str] = []
    for r in gaio:
        if not r.get("aio_exists"):
            continue
        q = r.get("query", "")
        if r.get("tcd_cited"):
            tcd_count += 1
            if q and q not in tcd_kws:
                tcd_kws.append(q)
        for domain in r.get("cited_domains", []):
            if "tcd" not in domain.lower():
                counts[domain] = counts.get(domain, 0) + 1
                if q and q not in kw_map.get(domain, []):
                    kw_map.setdefault(domain, []).append(q)

    ranking = sorted(
        [{"name": k, "count": v, "share": round(v / total_aio * 100, 1),
          "keywords": kw_map.get(k, [])}
         for k, v in counts.items()],
        key=lambda x: -x["count"]
    )
    ranking.append({
        "name": "TCD（自社）",
        "count": tcd_count,
        "share": round(tcd_count / total_aio * 100, 1),
        "keywords": tcd_kws,
        "is_tcd": True,
    })
    return ranking, total_aio


def gen_aio_competitor_hypothesis(ranking: list[dict], total_aio: int, period_label: str = "今週") -> str:
    """AIO競合ランキングの仮説テキストを2〜3文で生成する。"""
    if not ranking or total_aio == 0:
        return ""
    cited = [r for r in ranking if not r.get("is_tcd") and r["count"] > 0]
    tcd_entry = next((r for r in ranking if r.get("is_tcd")), None)
    parts = []
    if cited:
        top_names = "・".join(r["name"] for r in cited[:2])
        parts.append(
            f"{top_names}は、ブランディング会社の比較・一覧コンテンツを保有しており、"
            f"Googleが検索意図を一度で解決できるページとして評価しているとみられます。"
        )
    parts.append(
        "AIOでは、定義・選び方・比較情報が整理されたページが引用されるケースが見られます。"
    )
    if tcd_entry and tcd_entry["count"] == 0:
        parts.append(
            f"TCDは{period_label}引用0件。比較・一覧型コンテンツはAIO引用獲得に向けた有力な仮説の一つです。"
        )
    elif tcd_entry:
        parts.append(
            f"TCDは{period_label}AIシェア{tcd_entry['share']:.0f}%を獲得。"
            f"さらなる引用拡大には比較コンテンツの強化が有効です。"
        )
    return "".join(parts)


AIO_OPP_MIN_IMPRESSIONS = 100  # AIO Opportunity 抽出の最低Imp閾値
AIO_OPP_MAX_POSITION    = 15   # AIO Opportunity 抽出の最大順位


def compute_aio_opportunity(gaio: list[dict], cur_queries: pd.DataFrame) -> list[dict]:
    """AIO引用の可能性があるが未引用のキーワードを抽出する。
    対象は SerpAPI で観測済みのキーワードのみ（AIO有無・引用元は観測データからしか判定できないため）。
    criteria: impressions >= 100, average_position <= 15, tcd_cited_in_aio == False"""
    q_map: dict[str, Any] = {}
    if not cur_queries.empty:
        for _, row in cur_queries.iterrows():
            q_map[_normalize_query(row["query"])] = row

    out: list[dict] = []
    for r in gaio:
        q = r.get("query", "")
        if not q:
            continue
        row = q_map.get(_normalize_query(q))
        impressions = int(row["impressions"]) if row is not None else 0
        clicks      = int(row["clicks"])      if row is not None else 0
        ctr         = float(row["ctr"])       if row is not None else 0.0
        position    = float(row["position"])  if row is not None else 0.0

        if impressions < AIO_OPP_MIN_IMPRESSIONS or not (0 < position <= AIO_OPP_MAX_POSITION):
            continue
        if r.get("tcd_cited"):
            continue

        aio_present = bool(r.get("aio_exists"))
        competitor_domains = r.get("competitor_domains", [])[:3]

        if not aio_present:
            suggested_action = "SERP動向を継続監視（AIOが未表示のため引用機会自体が未発生）"
        elif ctr <= MAX_CTR_AIO:
            suggested_action = "title・description見直し"
        elif competitor_domains:
            suggested_action = "比較表追加"
        else:
            suggested_action = "FAQ追加"

        out.append({
            "keyword":            q,
            "impressions":        impressions,
            "clicks":             clicks,
            "ctr":                ctr,
            "position":           position,
            "aio_present":        aio_present,
            "competitor_domains": competitor_domains,
            "suggested_action":   suggested_action,
        })

    out.sort(key=lambda x: -x["impressions"])
    return out


def load_previous_gaio_snapshot(output_dir: str, exclude_date: str) -> list[dict] | None:
    """reports/google_aio 配下のキャッシュから exclude_date より前の最新スナップショットを読み込む。"""
    import json
    out_dir = Path(output_dir)
    if not out_dir.exists():
        return None
    files = sorted(p for p in out_dir.glob("*.json") if p.stem < exclude_date)
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def compare_point_watch(
    current: list[dict],
    previous: list[dict] | None,
    queries: list[str],
    domain_aliases: dict | None = None,
) -> list[dict]:
    """定点観測キーワードについて前週分と比較し、
    誰が増えたか（新規引用ドメイン）／AIO有無の変化／引用元が変わったか を算出する。
    前週分のキャッシュが正規化ロジック導入前に保存されていた場合に備え、比較直前に両者を正規化する。"""
    alias_lookup = build_domain_alias_lookup(domain_aliases)
    cur_map  = {r["query"]: r for r in current}
    prev_map = {r["query"]: r for r in (previous or [])}
    out = []
    for q in queries:
        r = cur_map.get(q)
        if r is None:
            continue
        prev = prev_map.get(q)
        cur_domains  = sorted({normalize_domain(d, alias_lookup) for d in r.get("cited_domains", [])})
        prev_domains = sorted({normalize_domain(d, alias_lookup) for d in prev.get("cited_domains", [])}) if prev else []
        new_domains  = sorted(set(cur_domains) - set(prev_domains))
        lost_domains = sorted(set(prev_domains) - set(cur_domains))
        citation_changed = prev is not None and cur_domains != prev_domains

        aio_now    = r.get("aio_exists")
        aio_before = prev.get("aio_exists") if prev else None
        if prev is None:
            aio_change = "初回観測"
        elif aio_now == aio_before:
            aio_change = "変化なし"
        elif aio_now and not aio_before:
            aio_change = "新たに出現"
        else:
            aio_change = "非表示化"

        citation_count_change = len(new_domains) + len(lost_domains)

        # SERP安定度: AIO有無の変化・引用元の増減件数から「変化なし／軽微／大きい」を判定
        if prev is None:
            serp_stability = "初回観測"
        elif aio_change != "変化なし":
            serp_stability = "大きい"
        elif citation_count_change == 0:
            serp_stability = "変化なし"
        elif citation_count_change <= 1:
            serp_stability = "軽微"
        else:
            serp_stability = "大きい"

        out.append({
            "query":                  q,
            "observed_at":            r.get("observed_at"),
            "prev_observed_at":       prev.get("observed_at") if prev else None,
            "aio_exists":             aio_now,
            "aio_change":             aio_change,
            "cited_domains":          cur_domains,
            "new_domains":            new_domains,
            "lost_domains":           lost_domains,
            "citation_changed":       citation_changed,
            "citation_count_change":  citation_count_change,
            "serp_stability":         serp_stability,
            "has_previous":           prev is not None,
        })
    return out


def point_watch_aio_presence_rate(point_watch: list[dict]) -> float | None:
    """定点観測キーワードのうち、AIOが表示されている割合を返す（0件ならNone）。"""
    if not point_watch:
        return None
    return sum(1 for r in point_watch if r["aio_exists"]) / len(point_watch) * 100


# ────────────────────────────────────────────────────────────────────
# フォーマット ユーティリティ
# ────────────────────────────────────────────────────────────────────

def fi(n: float) -> str:
    return f"{int(n):,}"

def fp(p: float) -> str:
    return f"{float(p) * 100:.1f}%"

def fpos(p: float) -> str:
    return f"{float(p):.1f}"

def fchg(c: float | None) -> str:
    if c is None:
        return "NEW"
    return f"+{c:.1f}%" if c > 0 else f"{c:.1f}%"

def fpos_chg(c: float) -> str:
    if abs(c) < 0.05:
        return "±0.0"
    return f"+{c:.1f}" if c > 0 else f"{c:.1f}"

def fctr_chg(c: float) -> str:
    return f"+{c * 100:.2f}pp" if c >= 0 else f"{c * 100:.2f}pp"

def pos_arrow(c: float) -> str:
    if c < -0.5:
        return "↑"   # 順位が上がった（数値が小さくなった）
    if c > 0.5:
        return "↓"
    return "→"

def aio_label(score: float) -> str:
    if score > 0.6:
        return f"🔴高 ({score:.2f})"
    if score > 0.4:
        return f"🟡中 ({score:.2f})"
    return f"🟢低 ({score:.2f})"


# ────────────────────────────────────────────────────────────────────
# 自動インサイト生成
# ────────────────────────────────────────────────────────────────────

def _calc_insights(data: dict) -> tuple[list[str], list[str], list[str]]:
    """インサイトを (good, bad, next_check) のリストで返す。**bold** 記法を含む。"""
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    good: list[str] = []
    bad: list[str] = []
    next_check: list[str] = []

    cc = pct_chg(tc["clicks"], tp["clicks"])
    if cc is not None:
        if cc > 5:
            good.append(f"総クリック数が前月比 **{cc:.1f}% 増加**")
        elif cc < -5:
            bad.append(f"総クリック数が前月比 **{abs(cc):.1f}% 減少**")

    pos_d = tc["position"] - tp["position"]
    if pos_d < -0.5:
        good.append(f"平均掲載順位が **{abs(pos_d):.1f}位改善**（{tp['position']:.1f} → {tc['position']:.1f}）")
    elif pos_d > 0.5:
        bad.append(f"平均掲載順位が **{pos_d:.1f}位低下**（{tp['position']:.1f} → {tc['position']:.1f}）")

    ctr_d = tc["ctr"] - tp["ctr"]
    if ctr_d > 0.005:
        good.append(f"CTR が **{ctr_d * 100:.2f}pp 改善**")
    elif ctr_d < -0.005:
        bad.append(f"CTR が **{abs(ctr_d) * 100:.2f}pp 低下**")
        next_check.append("CTR 低下の要因（SERP 変化・AI Overview の影響）を確認")

    high_aio = [
        a for a in data["aio_anomalies"]
        if a["aio"] > 0.6
        and a["impressions"] >= MIN_IMP_AIO
        and is_business_relevant(a["query"])
    ]
    if high_aio:
        qs = "、".join(f'「{a["query"]}」' for a in high_aio[:3])
        bad.append(f"**CTR確認候補**: {qs} でCTRが低い状態を確認")
        next_check.append(f"{qs} のSERP変化の可能性を確認")

    for r in data.get("key_kws", []):
        has_prev = r["prv"]["impressions"] > 0
        if not has_prev:
            continue
        if r["click_chg"] is not None and r["click_chg"] > 20:
            good.append(f"重点KW「**{r['query']}**」クリック {r['click_chg']:.0f}% 増")
        if r["pos_chg"] < -1:
            good.append(f"重点KW「**{r['query']}**」順位 {abs(r['pos_chg']):.1f}位改善")
        if r["pos_chg"] > 3:
            bad.append(f"重点KW「**{r['query']}**」順位 {r['pos_chg']:.1f}位変化")
            next_check.append(f"「{r['query']}」のコンテンツ・被リンク状況を確認")

    for r in data.get("lp_pages", []):
        has_prev = r["prv"]["impressions"] > 0
        if not has_prev:
            continue
        if r["click_chg"] is not None and r["click_chg"] > 20:
            good.append(f"CVページ「**{r['name']}**」クリック {r['click_chg']:.0f}% 増")
        if r["click_chg"] is not None and r["click_chg"] < -20:
            bad.append(f"CVページ「**{r['name']}**」クリック {abs(r['click_chg']):.0f}% 減")
            next_check.append(f"「{r['name']}」の SERP 表示・ページ内容を確認")

    return good, bad, next_check


# ────────────────────────────────────────────────────────────────────
# AI Insights / Action Items（ルールベース。将来 LLM 版を追加する場合は
# gen_ai_insights_llm() / gen_action_items_llm() を実装し、
# gen_ai_insights() / gen_action_items() の呼び出し先を切り替える）
# ────────────────────────────────────────────────────────────────────

def gen_ai_insights_rule_based(data: dict) -> list[str]:
    """Search Console と AIO を横断し、変化の背景仮説を閾値判定で生成する。"""
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    insights: list[str] = []

    imp_chg   = pct_chg(tc["impressions"], tp["impressions"])
    click_chg = pct_chg(tc["clicks"], tp["clicks"])
    pos_d     = tc["position"] - tp["position"]
    ctr_d     = tc["ctr"] - tp["ctr"]

    # クリック変化の主因診断（検索需要 vs 順位 vs CTR）
    if click_chg is not None:
        if click_chg < -5:
            if imp_chg is not None and imp_chg < -5 and abs(imp_chg) >= abs(pos_d) * 2:
                insights.append(
                    f"クリック減（{fchg(click_chg)}）の主因は検索需要（Impression）減少（{fchg(imp_chg)}）であり、"
                    "順位・CTRの悪化ではない可能性が高い。"
                )
            elif pos_d > 0.5:
                insights.append(f"クリック減（{fchg(click_chg)}）は平均順位の低下（{fpos_chg(pos_d)}）が主因と考えられる。")
            elif ctr_d < -0.005:
                insights.append(
                    f"クリック減（{fchg(click_chg)}）はCTR低下（{ctr_d*100:.2f}pp）が主因と考えられる。"
                    "SERP表示（AI Overviewなど）の変化を確認したい。"
                )
            else:
                insights.append(f"クリック減（{fchg(click_chg)}）の主因は特定の指標に集約できず、複合的な要因の可能性がある。")
        elif click_chg > 5:
            if pos_d < -0.5:
                insights.append(f"クリック増（{fchg(click_chg)}）は順位改善（{fpos_chg(pos_d)}）が寄与している。")
            elif imp_chg is not None and imp_chg > 5:
                insights.append(f"クリック増（{fchg(click_chg)}）は検索需要（Impression）の増加（{fchg(imp_chg)}）が寄与している。")

    # AIO状況の変化診断（日次モニターデータを優先、なければ定点観測にフォールバック）
    aio_summary = data.get("aio_daily_summary", {})
    if aio_summary and aio_summary.get("aio_query_count", 0) > 0:
        if aio_summary["aio_text_changed_count"] == 0:
            insights.append("観測期間中、AIO本文の変化は検出されず、SERP上のAIO表示は安定している。")
        else:
            insights.append(
                f"観測クエリのうち{aio_summary['aio_text_changed_count']}件でAIO本文の変化を検出。SERP構成が流動的な状態。"
            )
    else:
        pw = data.get("point_watch", [])
        if pw:
            changed = [r for r in pw if r.get("aio_change") not in (None, "変化なし")]
            if not changed:
                insights.append("定点観測4キーワードでAIOの変化なし。")
            else:
                qs = "、".join(f'「{r["query"]}」' for r in changed[:2])
                insights.append(f"{qs} でAIOの表示状況に変化あり。")

    # Definition記事の伸長診断
    growing_def = sorted(
        [
            r for r in data.get("def_pages", [])
            if r["prv"]["impressions"] > 0
            and pct_chg(r["cur"]["impressions"], r["prv"]["impressions"]) is not None
            and pct_chg(r["cur"]["impressions"], r["prv"]["impressions"]) > 20
        ],
        key=lambda r: -r["cur"]["impressions"],
    )
    if growing_def:
        top = growing_def[0]
        chg = pct_chg(top["cur"]["impressions"], top["prv"]["impressions"])
        insights.append(f"マガジン系「{top['name']}」のImpressionが{fchg(chg)}と伸長しており、コンテンツが検索需要を捉え始めている。")

    if not insights:
        insights.append("今期は指標間で明確な因果関係は検出されず、大きな変化のない安定期と考えられる。")

    return insights[:5]


def gen_ai_insights(data: dict) -> list[str]:
    """AI Insights セクション本文。現状はルールベース実装のみを使用する。"""
    return gen_ai_insights_rule_based(data)


def gen_action_items_rule_based(data: dict) -> list[str]:
    """来週やるべき実務タスクを閾値判定で抽出する。"""
    actions: list[str] = []
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]

    ctr_d = tc["ctr"] - tp["ctr"]
    if ctr_d < -0.005:
        actions.append("CTR低下の要因確認: 該当クエリのSERPを確認し、AI Overview・強調スニペット等の影響有無を切り分ける。")

    opp = data.get("aio_opportunity", [])
    if opp:
        top = opp[0]
        actions.append(f"AIO対策: 「{top['keyword']}」はAIO未引用のため、Definition記事の追加・強化を検討する。")

    ranking, _ = aio_competitor_ranking(data.get("google_aio", []))
    top_competitor = next((r for r in ranking if not r.get("is_tcd") and r["count"] > 0), None)
    if top_competitor:
        actions.append(
            f"競合対策: 「{top_competitor['name']}」がAIOで多く引用されている。"
            "比較記事など独自性の強いコンテンツの作成を検討する。"
        )

    dropping = sorted(
        [r for r in data.get("key_kws", []) if r["prv"]["impressions"] > 0 and r["pos_chg"] > 3],
        key=lambda r: -r["pos_chg"],
    )
    if dropping:
        top = dropping[0]
        actions.append(f"SERP確認: 重点KW「{top['query']}」の順位が{top['pos_chg']:.1f}位低下。競合ページ・SERP構成の変化を確認する。")

    growing_def = [
        r for r in data.get("def_pages", [])
        if r["prv"]["impressions"] > 0 and r["click_chg"] is not None and r["click_chg"] > 20
    ]
    if growing_def:
        actions.append(f"横展開: マガジン系「{growing_def[0]['name']}」が伸長中。関連テーマの記事追加を検討する。")

    if not actions:
        actions.append("大きな変化は検出されず。既存監視を継続し、特別なアクションは不要。")

    return actions[:5]


def gen_action_items(data: dict) -> list[str]:
    """Action Items セクション本文。現状はルールベース実装のみを使用する。"""
    return gen_action_items_rule_based(data)


def gen_insights(data: dict) -> str:
    good, bad, next_check = _calc_insights(data)
    lines: list[str] = ["### 伸びた項目"]
    lines += [f"- ✅ {g}" for g in good] if good else ["- 前月比で明確な改善項目は検出されませんでした。"]
    lines += ["", "### 落ちた項目"]
    lines += [f"- ⚠️ {b}" for b in bad] if bad else ["- 前月比で明確な悪化項目は検出されませんでした。"]
    lines += ["", "### 次月確認ポイント"]
    if not next_check:
        next_check.append("引き続きデータ蓄積を継続。特段の急変がなければ現状維持で問題なし。")
    lines += [f"- {n}" for n in next_check]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# Executive Summary（経営層向け要約・3〜5項目）
# ────────────────────────────────────────────────────────────────────

def gen_executive_summary(data: dict) -> list[str]:
    """経営層が最初の30秒で状況を把握できるよう、検索需要・順位・CTR・AIO状況・結論の
    5項目に絞った要約を返す。結論のみ gen_ai_insights() の仮説を採用する（他は事実ベース）。"""
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    period_label = "今週" if data.get("period_type") == "weekly" else "今月"
    items: list[str] = []

    # 1. 検索需要変化（Impression、事実）
    imp_chg = pct_chg(tc["impressions"], tp["impressions"])
    if imp_chg is not None:
        trend = "増加" if imp_chg > 5 else ("減少" if imp_chg < -5 else "横ばい")
        items.append(
            f"**検索需要**: Impression {fi(tc['impressions'])}件（前期比 {fchg(imp_chg)}、{trend}）。"
        )
    else:
        items.append(f"**検索需要**: Impression {fi(tc['impressions'])}件（初期観測フェーズのため前期比なし）。")

    # 2. 順位変化（事実）
    pos_d = tc["position"] - tp["position"]
    if tp["position"] > 0:
        trend = "改善" if pos_d < -0.5 else ("低下" if pos_d > 0.5 else "横ばい")
        items.append(f"**順位変化**: 平均掲載順位 {fpos(tc['position'])}位（{fpos_chg(pos_d)}、{trend}）。")
    else:
        items.append(f"**順位変化**: 平均掲載順位 {fpos(tc['position'])}位（初期観測フェーズのため前期比なし）。")

    # 3. CTR変化（事実）
    ctr_d = tc["ctr"] - tp["ctr"]
    if tp["impressions"] > 0:
        trend = "改善" if ctr_d > 0.005 else ("低下" if ctr_d < -0.005 else "横ばい")
        items.append(f"**CTR変化**: CTR {fp(tc['ctr'])}（{fctr_chg(ctr_d)}、{trend}）。")
    else:
        items.append(f"**CTR変化**: CTR {fp(tc['ctr'])}（初期観測フェーズのため前期比なし）。")

    # 4. AIO状況（事実。日次モニターデータがあれば優先し、なければ定点スナップショットにフォールバック）
    aio_summary = data.get("aio_daily_summary", {})
    if aio_summary and aio_summary.get("aio_query_count", 0) > 0:
        rate     = aio_summary["aio_exists_rate"]
        tcd_rate = aio_summary["tcd_cited_rate"]
        items.append(
            f"**AIO状況**: 観測{aio_summary['aio_query_count']}クエリ中 AIO表示率{rate*100:.0f}%、"
            f"TCD引用率{tcd_rate*100:.0f}%（本文変更 {aio_summary['aio_text_changed_count']}件）。"
        )
    else:
        gaio = data.get("google_aio", [])
        ranking, total_aio = aio_competitor_ranking(gaio)
        tcd_entry = next((r for r in ranking if r.get("is_tcd")), None)
        if gaio and any(r.get("status") != "API_KEY_MISSING" for r in gaio):
            if tcd_entry and total_aio > 0:
                items.append(
                    f"**AIO状況**: 観測AIO {total_aio}件中、TCD引用 {tcd_entry['count']}件"
                    f"（シェア{tcd_entry['share']:.0f}%）。"
                )
            else:
                items.append(f"**AIO状況**: {period_label}はAIOの表示自体が観測されず。")
        else:
            items.append("**AIO状況**: 観測データなし（SERPAPI_API_KEY未設定、またはAIO日次モニター未実行）。")

    # 5. 今週/月の結論（AI Insightsの最重要仮説を結論として提示）
    insights = gen_ai_insights(data)
    conclusion = insights[0] if insights else "特段の急変はなく、現状のモニタリングを継続。"
    items.append(f"**{period_label}の結論**: {conclusion}")

    return items[:5]


# ────────────────────────────────────────────────────────────────────
# Markdown レポート生成
# ────────────────────────────────────────────────────────────────────

def _kw_table_md(rows: list[dict]) -> str:
    if not rows:
        return "データなし\n"
    lines = [
        "| キーワード | クリック | 前期比 | IMP | CTR | 順位 | 順位変化 |",
        "|-----------|---------|--------|-----|-----|------|---------|",
    ]
    for r in rows:
        cd = r["cur"]
        arr = pos_arrow(r["pos_chg"])
        pos_abs = f"{abs(r['pos_chg']):.1f}" if abs(r["pos_chg"]) >= 0.05 else "0.0"
        lines.append(
            f"| {r['query']} "
            f"| {fi(cd['clicks'])} | {fchg(r['click_chg'])} "
            f"| {fi(cd['impressions'])} | {fp(cd['ctr'])} "
            f"| {fpos(cd['position'])} | {arr}{pos_abs} |"
        )
    return "\n".join(lines) + "\n"


def _page_table_md(rows: list[dict], site_url: str) -> str:
    if not rows:
        return "データなし\n"
    base = site_url.rstrip("/")
    lines = [
        "| ページ名 | クリック | 前期比 | IMP | CTR | 順位 | AIOスコア |",
        "|---------|---------|--------|-----|-----|------|---------|",
    ]
    for r in rows:
        cd = r["cur"]
        short = r["url"].replace(base, "")
        lines.append(
            f"| [{r['name']}]({r['url']}) "
            f"| {fi(cd['clicks'])} | {fchg(r['click_chg'])} "
            f"| {fi(cd['impressions'])} | {fp(cd['ctr'])} "
            f"| {fpos(cd['position'])} | {aio_label(r['aio_score'])} |"
        )
    return "\n".join(lines) + "\n"


_AIO_DAILY_STATUS_LABEL_MD = {
    "OK_WITH_AIO": "AIOあり", "OK_NO_AIO": "AIOなし",
    "FETCH_ERROR": "取得失敗", "API_KEY_MISSING": "APIキー未設定",
}
_AIO_DOMAIN_STATUS_LABEL_MD = {"new": "🆕 新規", "lost": "⚠️ 消失", "": "-"}


def _aio_daily_summary_md(summary: dict) -> str:
    if not summary or summary.get("aio_query_count", 0) == 0:
        return "AIO日次データ未取得（data/aio/looker/aio_daily.csv が存在しないか、対象期間のデータがありません）。\n"
    rate     = summary["aio_exists_rate"]
    tcd_rate = summary["tcd_cited_rate"]
    lines = [
        f"- 観測クエリ数: {summary['aio_query_count']}",
        f"- AIO表示率: {rate*100:.0f}%" if rate is not None else "- AIO表示率: -",
        f"- TCD引用率: {tcd_rate*100:.0f}%" if tcd_rate is not None else "- TCD引用率: -",
        f"- AIO本文変更件数: {summary['aio_text_changed_count']}",
    ]
    if summary.get("error_count"):
        lines.append(f"- エラー件数: {summary['error_count']}")
    return "\n".join(lines) + "\n"


def _aio_query_daily_table_md(query_level: list[dict]) -> str:
    if not query_level:
        return "AIO日次データ未取得。\n"
    lines = [
        "| クエリ | AIO表示日数 | AIO表示率 | TCD引用日数 | TCD言及日数 | 最新ステータス | 本文変更回数 |",
        "|-------|-----------|----------|-----------|-----------|--------------|------------|",
    ]
    for r in query_level:
        rate   = f"{r['aio_exists_rate']*100:.0f}%" if r["aio_exists_rate"] is not None else "-"
        status = _AIO_DAILY_STATUS_LABEL_MD.get(r["latest_status"], r["latest_status"])
        lines.append(
            f"| {r['query']} | {r['aio_exists_days']} | {rate} "
            f"| {r['tcd_cited_days']} | {r['tcd_mentioned_days']} "
            f"| {status} | {r['aio_text_changed_count']} |"
        )
    return "\n".join(lines) + "\n"


def _aio_domain_trend_table_md(trend: dict) -> str:
    rows = (trend or {}).get("top_domains", []) + (trend or {}).get("lost_rows", [])
    if not rows:
        return "AIO引用ドメインデータ未取得。\n"
    lines = [
        "| ドメイン | 引用回数 | 対象クエリ | 初出日 | 最終観測日 | 状態 |",
        "|---------|---------|-----------|-------|-----------|------|",
    ]
    for r in rows:
        status  = _AIO_DOMAIN_STATUS_LABEL_MD.get(r["status"], r["status"])
        queries = "・".join(r["related_queries"][:3]) or "-"
        lines.append(
            f"| {r['domain']} | {r['cited_count']} | {queries} "
            f"| {r['first_seen']} | {r['last_seen']} | {status} |"
        )
    return "\n".join(lines) + "\n"


def _aio_text_change_summary_md(changes: dict) -> str:
    if not changes or changes.get("changed_count", 0) == 0:
        return "対象期間中、AIO本文の変化は検出されませんでした。\n"
    lines = [f"**本文変更件数**: {changes['changed_count']}件", "", "**直近の変更クエリ:**"]
    for c in changes["changed_queries"]:
        lines.append(f"- {c['date']}: 「{c['query']}」")
    return "\n".join(lines) + "\n"


def gen_markdown(data: dict, config: dict) -> str:
    (cs, ce), (ps, pe) = data["periods"]
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    site_url = config["site_url"]
    top_n = config.get("report", {}).get("top_n", 20)

    report_month = datetime.strptime(cs, "%Y-%m-%d").strftime("%Y年%m月")
    cur_days = (datetime.strptime(ce, "%Y-%m-%d") - datetime.strptime(cs, "%Y-%m-%d")).days + 1
    prv_days = (datetime.strptime(pe, "%Y-%m-%d") - datetime.strptime(ps, "%Y-%m-%d")).days + 1

    md: list[str] = []

    # ── ヘッダー ──
    md += [
        f"# TCD.jp SEO/AIO 月次レポート — {report_month}",
        "",
        f"| | 期間 |",
        f"|---|---|",
        f"| **前月** | {cs} 〜 {ce}（{cur_days}日間） |",
        f"| **前々月** | {ps} 〜 {pe}（{prv_days}日間） |",
        f"| **生成日時** | {datetime.now().strftime('%Y年%m月%d日 %H:%M')} |",
        "",
        "---",
        "",
    ]

    # ── Executive Summary ──
    md += ["## Executive Summary", ""]
    for item in gen_executive_summary(data):
        md.append(f"- {item}")
    md += ["", "---", ""]

    # ── AI Insights（ルールベース仮説） ──
    md += [
        "## AI Insights",
        "",
        "> Search Console × AIO を横断した変化の背景仮説（ルールベース生成）。",
        "",
    ]
    for item in gen_ai_insights(data):
        md.append(f"- {item}")
    md += ["", "---", ""]

    # ── Action Items（来週やること） ──
    md += [
        "## Action Items",
        "",
        "> 来週やるべき実務タスク（優先度順）。",
        "",
    ]
    for item in gen_action_items(data):
        md.append(f"- {item}")
    md += ["", "---", ""]

    # ── 1. サイト全体サマリー ──
    md += ["## 1. サイト全体サマリー", ""]
    md += [
        "| 指標 | 前月 | 前々月 | 増減 | 増減率 |",
        "|------|------|------|------|--------|",
    ]

    def summary_row(label, key, fmt_fn, diff_fn, chg_fn):
        cv, pv = tc[key], tp[key]
        diff = cv - pv
        return f"| {label} | {fmt_fn(cv)} | {fmt_fn(pv)} | {diff_fn(diff)} | {chg_fn(cv, pv)} |"

    md.append(summary_row(
        "クリック数", "clicks",
        fi,
        lambda d: f"+{fi(d)}" if d > 0 else fi(d),
        lambda c, p: fchg(pct_chg(c, p)),
    ))
    md.append(summary_row(
        "インプレッション", "impressions",
        fi,
        lambda d: f"+{fi(d)}" if d > 0 else fi(d),
        lambda c, p: fchg(pct_chg(c, p)),
    ))
    # CTR は pp で表示
    md.append(
        f"| CTR | {fp(tc['ctr'])} | {fp(tp['ctr'])} "
        f"| {fctr_chg(tc['ctr'] - tp['ctr'])} | {fchg(pct_chg(tc['ctr'], tp['ctr']))} |"
    )
    # 順位は小さい方が良いので矢印逆
    pos_d = tc["position"] - tp["position"]
    pos_trend = "↑改善" if pos_d < -0.05 else ("↓低下" if pos_d > 0.05 else "→横ばい")
    md.append(
        f"| 平均掲載順位 | {fpos(tc['position'])} | {fpos(tp['position'])} "
        f"| {fpos_chg(pos_d)} | {pos_trend} |"
    )
    md.append("")

    # ── 2. 重点キーワード ──
    md += ["## 2. 重点キーワード分析", ""]
    md.append(_kw_table_md(data["key_kws"]))

    # ── 3. Google AI Overview 観測 ──
    md += ["## 3. Google AI Overview 観測", ""]
    gaio = data.get("google_aio", [])
    if not gaio or all(r.get("status") == "API_KEY_MISSING" for r in gaio):
        md.append("観測データなし（SERPAPI_API_KEY 未設定または無効）。\n")
    else:
        md += [
            "| クエリ | ステータス | AIO有無 | TCD引用 | TCD言及 | 競合引用ドメイン |",
            "|--------|-----------|--------|--------|--------|----------------|",
        ]
        for r in gaio:
            aio  = "✅ あり" if r.get("aio_exists") else ("⬜ なし" if r.get("aio_exists") is False else "?")
            tcd_c = "✅" if r.get("tcd_cited") else "なし"
            tcd_m = "✅" if r.get("tcd_mentioned") else "なし"
            comp  = "・".join(r.get("competitor_domains", [])[:3]) or "-"
            md.append(f"| {r['query']} | {r.get('status','')} | {aio} | {tcd_c} | {tcd_m} | {comp} |")
        md.append("")
        for r in gaio:
            if r.get("aio_exists"):
                md.append(f"### {r['query']}")
                md.append("")
                if r.get("aio_text"):
                    md.append(f"**AIO本文抜粋**: {r['aio_text'][:300]}")
                    md.append("")
                if r.get("cited_urls"):
                    md.append("**引用URL一覧:**")
                    for u in r["cited_urls"]:
                        md.append(f"- {u}")
                    md.append("")
                if r.get("tcd_cited_urls"):
                    md.append("**TCD引用URL:**")
                    for u in r["tcd_cited_urls"]:
                        md.append(f"- {u}")
                    md.append("")
                if r.get("competitor_domains"):
                    md.append(f"**競合引用ドメイン**: {', '.join(r['competitor_domains'])}")
                    md.append("")

    # ── 4. AIO競合ランキング ──
    md += ["## 4. AIO競合ランキング", ""]
    period_label = "今週" if data.get("period_type") == "weekly" else "今月"
    ranking, total_aio = aio_competitor_ranking(data.get("google_aio", []))
    md.append(f"### {period_label}引用された企業（観測AIO: {total_aio}件）")
    md.append("")
    if ranking:
        md += ["| 企業 | 対象キーワード | AIシェア |", "|------|-------------|---------|"]
        for item in ranking:
            kw_txt = "、".join(item.get("keywords", [])) or "—"
            share_txt = f'{item["count"]}件 ({item["share"]:.0f}%)'
            md.append(f"| {item['name']} | {kw_txt} | {share_txt} |")
        md.append("")
        hypothesis = gen_aio_competitor_hypothesis(ranking, total_aio, period_label)
        if hypothesis:
            md.append(f"> {hypothesis}")
            md.append("")
    else:
        md.append("AIOに引用された企業は検出されませんでした。\n")

    # ── 5. 定点SERP観測（重点4キーワード） ──
    md += ["## 5. 定点SERP観測（重点4キーワード）", ""]
    md.append(
        "> 「ブランディング会社 東京/大阪」「インナーブランディング会社 東京/大阪」の4キーワードについて、"
        "Google検索結果を毎週定点観測しています（誰が増えたか／AIOの有無／引用元の変化）。"
    )
    md.append("")
    pw = data.get("point_watch", [])
    if not pw:
        md.append("観測データなし（SERPAPI_API_KEY 未設定、または point_watch_queries 未設定）。\n")
    else:
        rate = point_watch_aio_presence_rate(pw)
        if rate is not None:
            md.append(f"**AIO表示率（定点観測4キーワード中）**: {rate:.0f}%")
            md.append("")
        md += [
            "| キーワード | AIO有無 | AIO変化 | 引用元変化 | SERP安定度 | 新規引用ドメイン（誰が増えたか） | 消失ドメイン |",
            "|-----------|--------|--------|-----------|-----------|--------------------------------|------------|",
        ]
        for r in pw:
            aio = "✅ あり" if r["aio_exists"] else "⬜ なし"
            if not r["has_previous"]:
                cite_chg = "初回観測"
            else:
                cite_chg = "✅ 変化あり" if r["citation_changed"] else "変化なし"
            new_d  = "・".join(r["new_domains"]) or "-"
            lost_d = "・".join(r["lost_domains"]) or "-"
            md.append(f"| {r['query']} | {aio} | {r['aio_change']} | {cite_chg} | {r['serp_stability']} | {new_d} | {lost_d} |")
        md.append("")

        highlights = [r for r in pw if r["new_domains"] or r["citation_changed"] or r["aio_change"] not in ("変化なし",)]
        if highlights:
            md.append("**今週の変化点:**")
            for r in highlights:
                notes = []
                if r["aio_change"] != "変化なし":
                    notes.append(f"AIOが{r['aio_change']}")
                if r["new_domains"]:
                    notes.append(f"新規引用: {'・'.join(r['new_domains'])}")
                if r["lost_domains"]:
                    notes.append(f"引用消失: {'・'.join(r['lost_domains'])}")
                md.append(f"- 「{r['query']}」: {' / '.join(notes)}")
            md.append("")

    # ── 6. AIO Opportunity ──
    md += [
        "## 6. AIO Opportunity",
        "",
        "> AIOに引用される可能性がありながら未引用のキーワード（Imp 100以上・順位15位以内・TCD未引用）を抽出しています。",
        "",
    ]
    opp = data.get("aio_opportunity", [])
    if opp:
        md += [
            "| キーワード | IMP | クリック | CTR | 順位 | AIO表示 | 競合引用ドメイン | 推奨アクション |",
            "|-----------|-----|---------|-----|------|--------|----------------|---------------|",
        ]
        for o in opp:
            aio_txt = "✅ あり" if o["aio_present"] else "⬜ なし"
            comp = "・".join(o["competitor_domains"]) or "-"
            md.append(
                f"| {o['keyword']} | {fi(o['impressions'])} | {fi(o['clicks'])} "
                f"| {fp(o['ctr'])} | {fpos(o['position'])} | {aio_txt} | {comp} | {o['suggested_action']} |"
            )
        md.append("")
    else:
        md.append("該当するキーワードは検出されませんでした。\n")

    # ── 7. AIO Daily Summary ──
    md += [
        "## 7. AIO Daily Summary",
        "",
        "> aio_daily_monitor.py（毎朝6時実行）が観測した日次AIOデータを、本レポートと同じ対象期間で集計しています。",
        "",
    ]
    md.append(_aio_daily_summary_md(data.get("aio_daily_summary", {})))

    # ── 8. クエリ別AIO日次モニター ──
    md += ["## 8. クエリ別AIO日次モニター", ""]
    md.append(_aio_query_daily_table_md(data.get("aio_daily_summary", {}).get("query_level", [])))

    # ── 9. 引用ドメイン推移 ──
    md += ["## 9. 引用ドメイン推移", ""]
    md.append(_aio_domain_trend_table_md(data.get("aio_domain_trend", {})))

    # ── 10. AIO本文変化サマリー ──
    md += [
        "## 10. AIO本文変化サマリー",
        "",
        "> 本文全文は data/aio/daily/YYYY-MM-DD.json を参照してください。ここではハッシュ変化のみを扱います。",
        "",
    ]
    md.append(_aio_text_change_summary_md(data.get("aio_text_changes", {})))

    # ── 11. サービス系 ──
    md += ["## 11. サービス系", ""]
    md.append(_page_table_md(data["service_pages"], site_url))

    # ── 12. サービスDefinition系 ──
    md += ["## 12. サービスDefinition系", ""]
    md.append(_page_table_md(data["aio_pages"], site_url))

    # ── 13. マガジン系 ──
    md += ["## 13. マガジン系", ""]
    md.append(_page_table_md(data["def_pages"], site_url))

    # ── 14. 重要ページ ──
    md += ["## 14. 重要ページ分析", ""]
    md.append(_page_table_md(data["lp_pages"], site_url))

    # ── 15. デバイス別 ──
    md += ["## 15. デバイス別分析", ""]
    devs = data["devices"]
    if devs:
        md += [
            "| デバイス | クリック | IMP | CTR | 平均順位 |",
            "|---------|---------|-----|-----|---------|",
        ]
        for d in sorted(devs, key=lambda x: -x["clicks"]):
            md.append(
                f"| {d['device'].capitalize()} "
                f"| {fi(d['clicks'])} | {fi(d['impressions'])} "
                f"| {fp(d['ctr'])} | {fpos(d['position'])} |"
            )
        md.append("")
    else:
        md.append("データなし\n")

    # ── 12. CTR確認候補クエリ（参考） ──
    md += [
        "## 16. 参考：CTR確認候補クエリ",
        "",
        "> 掲載順位に対してCTRが低いクエリ一覧。SERP変化・AI Overview影響の可能性があります。断定ではなく次月以降の確認ポイントとしてご参照ください。",
        "",
    ]
    anom = data["aio_anomalies"]
    if anom:
        md += [
            "| クエリ | IMP | クリック | CTR | 順位 | AIOスコア |",
            "|-------|-----|---------|-----|------|---------|",
        ]
        for a in anom:
            md.append(
                f"| {a['query']} "
                f"| {fi(a['impressions'])} | {fi(a['clicks'])} "
                f"| {fp(a['ctr'])} | {fpos(a['position'])} "
                f"| {aio_label(a['aio'])} |"
            )
        md.append("")
    else:
        md.append("AIOシグナル（閾値超え）は検出されませんでした。\n")

    # ── 13. 上位クエリ ──
    md += [f"## 17. 上位クエリ TOP{top_n}", ""]
    tq = data["top_queries"]
    if tq:
        md += [
            "| # | クエリ | クリック | 前期比 | IMP | CTR | 順位 |",
            "|---|-------|---------|--------|-----|-----|------|",
        ]
        for i, r in enumerate(tq, 1):
            cd = r["cur"]
            md.append(
                f"| {i} | {r['query']} "
                f"| {fi(cd['clicks'])} | {fchg(r['click_chg'])} "
                f"| {fi(cd['impressions'])} | {fp(cd['ctr'])} "
                f"| {fpos(cd['position'])} |"
            )
        md.append("")
    else:
        md.append("データなし\n")

    # ── 14. 上位ページ ──
    md += [f"## 18. 上位ページ TOP{top_n}", ""]
    tp_list = data["top_pages"]
    base = site_url.rstrip("/")
    if tp_list:
        md += [
            "| # | ページ | クリック | 前期比 | IMP | CTR | 順位 |",
            "|---|-------|---------|--------|-----|-----|------|",
        ]
        for i, r in enumerate(tp_list, 1):
            cd = r["cur"]
            short = r["page"].replace(base, "") or "/"
            md.append(
                f"| {i} | [{short}]({r['page']}) "
                f"| {fi(cd['clicks'])} | {fchg(r['click_chg'])} "
                f"| {fi(cd['impressions'])} | {fp(cd['ctr'])} "
                f"| {fpos(cd['position'])} |"
            )
        md.append("")
    else:
        md.append("データなし\n")

    # ── 15. 新規・更新ページのインデックス監視 ──
    md += [
        "## 19. 新規・更新ページのインデックス監視",
        "",
        "> 新規Definition記事・更新ページがSearch Console上に出始めたかを追跡しています"
        "（config/aio_monitor.yml の new_index_watch に追加すると対象になります）。",
        "",
    ]
    idx_mon = data.get("index_monitoring", [])
    _STATUS_LABEL_MD = {
        "not_seen":           "未検出",
        "indexed_no_impression": "インデックス済・表示回数0",
        "impression_started": "表示開始",
        "ranking_started":    "クリック発生",
    }
    if idx_mon:
        md += [
            "| ページ | 公開・更新日 | 初回表示検出 | IMP | クリック | 平均順位 | ステータス |",
            "|-------|-------------|-------------|-----|---------|---------|----------|",
        ]
        for p in idx_mon:
            first_seen = p["first_seen_in_search_console"] or "-"
            status_txt = _STATUS_LABEL_MD.get(p["status"], p["status"])
            md.append(
                f"| [{p['page_title']}]({p['url']}) | {p['published_or_updated_date'] or '-'} "
                f"| {first_seen} | {fi(p['impressions'])} | {fi(p['clicks'])} "
                f"| {fpos(p['average_position'])} | {status_txt} |"
            )
        md.append("")
    else:
        md.append("監視対象ページが未登録です（config/aio_monitor.yml の new_index_watch を設定してください）。\n")

    # ── 16. 次月確認ポイント ──
    md += ["## 20. 次月確認ポイント・改善推奨", ""]
    md.append(gen_insights(data))
    md.append("")

    return "\n".join(md)


# ────────────────────────────────────────────────────────────────────
# HTML レポート生成（デザイン仕様版）
# ────────────────────────────────────────────────────────────────────

def gen_html(data: dict, config: dict) -> str:
    """月次SEO/AIOレポートをデザイン仕様に沿ったHTMLで生成する。"""
    (cs, ce), (ps, pe) = data["periods"]
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    period_type     = data.get("period_type", "monthly")
    generated_at    = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    has_global_prev = tp["impressions"] > 0

    if period_type == "weekly":
        report_title = f"{cs} 〜 {ce} 週次 SEO / AIO 比較レポート"
        header_label = "TCD SEO / AIO Weekly Report"
        cur_label    = "直近7日（対象）"
        prv_label    = "前の7日（比較）"
        report_month = f"{cs}週"
    else:
        report_month = datetime.strptime(cs, "%Y-%m-%d").strftime("%Y年%m月")
        report_title = f"{report_month} SEO / AIO 効果測定レポート"
        header_label = "TCD SEO / AIO Monthly Report"
        cur_label    = "前月（対象）"
        prv_label    = "前々月（比較）"

    NAVY    = "#0b2345"
    CYAN    = "#00B4D8"
    BG      = "#f6f7f9"
    TEXT    = "#333333"
    MUTED   = "#777777"
    BORDER  = "#e2e6ea"
    INFO_BG = "#f0f9ff"
    HYPO_BG = "#f8fafc"
    UP      = "#00a878"
    DOWN    = "#e05050"

    def esc(s: Any) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def bold_html(s: str) -> str:
        """**text** 記法を <strong> に変換する（他はエスケープ済み）。"""
        return re.sub(r"\*\*(.+?)\*\*", rf'<strong style="color:{NAVY};">\1</strong>', esc(s))

    def chg_span(val: float | None) -> str:
        if val is None:
            return f'<span style="color:{MUTED};font-size:14px;">NEW</span>'
        color  = UP if val > 1 else (DOWN if val < -1 else MUTED)
        prefix = "+" if val > 0 else ""
        return f'<span style="color:{color};font-weight:700;">{prefix}{val:.1f}%</span>'

    def pos_span(val: float) -> str:
        if abs(val) < 0.05:
            return f'<span style="color:{MUTED};">→0.0</span>'
        color = UP if val < -0.5 else (DOWN if val > 0.5 else MUTED)
        arrow = "↑" if val < -0.5 else ("↓" if val > 0.5 else "→")
        return f'<span style="color:{color};font-weight:700;">{arrow}{abs(val):.1f}</span>'

    def sec_title(num: str, title: str) -> str:
        return (
            f'<tr><td style="padding:28px 0 10px;border-top:1px solid {BORDER};">'
            f'<table cellpadding="0" cellspacing="0"><tr>'
            f'<td style="background:{CYAN};width:3px;border-radius:2px;">&nbsp;</td>'
            f'<td style="padding-left:10px;">'
            f'<div style="color:{NAVY};font-size:19px;font-weight:700;">{esc(num)}. {esc(title)}</div>'
            f'</td></tr></table></td></tr>'
        )

    def info_box(html: str) -> str:
        return (
            f'<tr><td style="padding-bottom:16px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 6px 6px 0;'
            f'padding:16px 20px;color:{TEXT};font-size:18px;line-height:1.8;">{html}</div></td></tr>'
        )

    def finding_row(text: str) -> str:
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 5px 5px 0;'
            f'padding:11px 16px;color:{TEXT};font-size:16px;line-height:1.6;">&#128269; {esc(text)}</div></td></tr>'
        )

    def hypothesis_row(text: str) -> str:
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="background:{HYPO_BG};border-left:4px solid #cbd5e1;border-radius:0 5px 5px 0;'
            f'padding:11px 16px;color:{TEXT};font-size:16px;line-height:1.6;">&#128270; {esc(text)}</div></td></tr>'
        )

    def check_row(text: str) -> str:
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="padding:4px 0 4px 12px;border-left:3px solid {BORDER};'
            f'color:{TEXT};font-size:16px;line-height:1.6;">&#8250; {esc(text)}</div></td></tr>'
        )

    def th(label: str, align: str = "left", w: str = "") -> str:
        ws = f' width="{w}"' if w else ""
        return (
            f'<th{ws} style="background:{NAVY};color:#fff;font-size:14px;font-weight:600;'
            f'padding:8px 10px;text-align:{align};white-space:nowrap;letter-spacing:.3px;">{esc(label)}</th>'
        )

    def td_cell(content: str, align: str = "left") -> str:
        return (
            f'<td style="padding:7px 10px;font-size:15px;color:{TEXT};'
            f'text-align:{align};border-bottom:1px solid {BORDER};vertical-align:middle;">{content}</td>'
        )

    def wrap_table(hdr: str, body: str) -> str:
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;border:1px solid {BORDER};border-radius:6px;overflow:hidden;margin-bottom:4px;">'
            f'<thead><tr>{hdr}</tr></thead><tbody>{body}</tbody></table>'
        )

    # ── Imp順ソート ＋ Imp/Click=0 をHTMLから除外 ──
    def sort_and_filter(items: list[dict]) -> tuple[list[dict], int]:
        active = [r for r in items if r["cur"]["clicks"] > 0 or r["cur"]["impressions"] > 0]
        active.sort(key=lambda x: -x["cur"]["impressions"])
        return active, len(items) - len(active)

    def hidden_note(hidden: int, total: int, unit: str = "件") -> str:
        if hidden == 0:
            return ""
        return (
            f'<tr><td style="padding:2px 0 10px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'※ {total}{unit}中 {total - hidden}{unit}を表示（Imp=0の{hidden}{unit}は省略）</p>'
            f'</td></tr>'
        )

    kw_active,  kw_hidden  = sort_and_filter(data["key_kws"])
    svc_active, svc_hidden = sort_and_filter(data["service_pages"])
    aio_active, aio_hidden = sort_and_filter(data["aio_pages"])
    def_active, def_hidden = sort_and_filter(data["def_pages"])
    lp_active,  lp_hidden  = sort_and_filter(data["lp_pages"])

    # ── 2行レイアウトテーブル（ベースライン月は比較列なし） ──
    def two_row_table(items: list[dict], name_fn) -> str:
        cmp_label = "前週比" if period_type == "weekly" else "前月比"
        if has_global_prev:
            col_count = 6
            hdr = (
                th("IMP", "right", "65") + th("Click", "right", "55") +
                th("CTR", "right", "55") + th("順位",  "right", "55") +
                th(f"{cmp_label}(Click)", "right", "90") + th(f"{cmp_label}(順位)", "right", "90")
            )
        else:
            col_count = 4
            hdr = (
                th("IMP",   "right", "80") + th("Click", "right", "70") +
                th("CTR",   "right", "70") + th("順位",  "right", "70")
            )
        rows = ""
        for r in items:
            cd      = r["cur"]
            has_cur = cd["impressions"] > 0

            rows += (
                f'<tr><td colspan="{col_count}" style="padding:9px 12px;font-size:16px;'
                f'font-weight:700;border-bottom:1px solid {BORDER};color:{NAVY};">'
                f'{name_fn(r)}</td></tr>\n'
            )
            ctr_val = fp(cd["ctr"])        if has_cur else f'<span style="color:{MUTED};">-</span>'
            pos_val = fpos(cd["position"]) if has_cur else f'<span style="color:{MUTED};">-</span>'
            data_row = (
                '<tr style="background:#f8fafc;">'
                + td_cell(fi(cd["impressions"]), "right")
                + td_cell(fi(cd["clicks"]),      "right")
                + td_cell(ctr_val,               "right")
                + td_cell(pos_val,               "right")
            )
            if has_global_prev:
                has_prev  = r["prv"]["impressions"] > 0
                ref_span  = f'<span style="color:{MUTED};font-size:14px;">参考値</span>'

                # クリック前月比: 前期クリック < 3 は参考値
                cl_cell = td_cell(
                    ref_span if r["prv"]["clicks"] < 3 else chg_span(r["click_chg"]),
                    "right"
                )
                # 順位変化: 前期なし → 比較対象なし / 現期Imp < 30 → 参考値
                if not has_prev:
                    pos_cell = td_cell(f'<span style="color:{MUTED};font-size:14px;">比較対象なし</span>', "right")
                elif cd["impressions"] < MIN_IMP_FINDING:
                    pos_cell = td_cell(ref_span, "right")
                else:
                    pos_cell = td_cell(pos_span(r["pos_chg"]), "right")

                data_row += cl_cell + pos_cell
            rows += data_row + "</tr>\n"
        return wrap_table(hdr, rows)

    # ── 今月の発見（実数値あり） ──
    def gen_findings() -> list[str]:
        found = []

        if kw_active:
            top = kw_active[0]
            cd  = top["cur"]
            vals = [f'{fi(cd["impressions"])} Imp']
            if cd["clicks"] > 0:
                vals.append(f'{fi(cd["clicks"])} Click')
            if cd["position"] > 0:
                vals.append(f'順位 {cd["position"]:.1f} 位')
            found.append(f'重点KWでは「{top["query"]}」が {" ・ ".join(vals)} で最も露出している。')

        if svc_active:
            top = svc_active[0]
            cd  = top["cur"]
            vals = [f'{fi(cd["impressions"])} Imp']
            if cd["position"] > 0:
                vals.append(f'順位 {cd["position"]:.1f} 位')
            found.append(f'サービス系では「{top["name"]}」が {" ・ ".join(vals)} で先行している。')

        if aio_active:
            top = aio_active[0]
            cd  = top["cur"]
            vals = [f'{fi(cd["impressions"])} Imp']
            if cd["position"] > 0:
                vals.append(f'順位 {cd["position"]:.1f} 位')
            found.append(f'サービスDefinition系では「{top["name"]}」が {" ・ ".join(vals)} で先行している。')

        if def_active:
            top = def_active[0]
            cd  = top["cur"]
            found.append(f'マガジン系では「{top["name"]}」が {fi(cd["impressions"])} Imp で最も検索露出を獲得している。')

        if lp_active:
            top = lp_active[0]
            cd  = top["cur"]
            vals = [f'{fi(cd["impressions"])} Imp']
            if cd["clicks"] > 0:
                vals.append(f'{fi(cd["clicks"])} Click')
            found.append(f'「{top["name"]}」は {" ・ ".join(vals)} を記録している（初期値）。')

        # 順位10位以内到達（Rule 5: imp>=30）
        top10 = sorted(
            [r for r in kw_active + svc_active + aio_active + def_active
             if 0 < r["cur"]["position"] <= 10 and r["cur"]["impressions"] >= MIN_IMP_FINDING],
            key=lambda x: x["cur"]["position"]
        )
        if top10:
            best = top10[0]
            cd   = best["cur"]
            name = best.get("query") or best.get("name", "")
            found.append(f'「{name}」が順位 {cd["position"]:.1f} 位で検索上位10位以内に到達している（初期値）。')

        if not found:
            found.append("データ蓄積中。次月以降の比較対象として初期観測フェーズの値を記録した。")
        return found[:5]

    # ── 今月の仮説（観察ベース・スクリーニングルール適用） ──
    def gen_hypothesis() -> list[str]:
        hypos = []

        # CTR確認候補（Rule 2: imp>=50, pos<=15, ctr<=1%）
        for r in data["key_kws"]:
            cd = r["cur"]
            if cd["impressions"] >= MIN_IMP_AIO and 0 < cd["position"] <= MAX_POS_AIO:
                expected = EXPECTED_CTR_BY_POS.get(max(1, min(10, round(cd["position"]))), 0.02)
                if cd["ctr"] < expected * 0.5 and cd["ctr"] <= MAX_CTR_AIO:
                    hypos.append(
                        f'「{r["query"]}」は順位 {cd["position"]:.0f} 位・{fi(cd["impressions"])} Imp に対して'
                        f'CTRは {cd["ctr"]*100:.1f}% にとどまっている。'
                    )
        for r in data["service_pages"] + data["aio_pages"]:
            cd = r["cur"]
            if cd["impressions"] >= MIN_IMP_AIO and 0 < cd["position"] <= MAX_POS_AIO:
                expected = EXPECTED_CTR_BY_POS.get(max(1, min(10, round(cd["position"]))), 0.02)
                if cd["ctr"] < expected * 0.4 and cd["ctr"] <= MAX_CTR_AIO:
                    hypos.append(
                        f'「{r["name"]}」は {fi(cd["impressions"])} Imp・順位 {cd["position"]:.0f} 位に対して'
                        f'クリック率が {cd["ctr"]*100:.1f}% にとどまっている。'
                    )

        # Definition記事: imp>=30, クリックゼロ（Rule 1）
        def_zero = sorted(
            [r for r in data["def_pages"]
             if r["cur"]["impressions"] >= MIN_IMP_FINDING and r["cur"]["clicks"] == 0],
            key=lambda x: -x["cur"]["impressions"]
        )
        if def_zero:
            r = def_zero[0]
            hypos.append(
                f'「{r["name"]}」は {fi(r["cur"]["impressions"])} Imp の表示があるが、クリックは発生していない。'
            )

        # 順位変化確認候補（Rule 4: 週次モード、imp>=50、変化>=3位）
        if has_global_prev:
            tracked = (
                [(r, r.get("query", "")) for r in data["key_kws"]] +
                [(r, r.get("name",  "")) for r in data["service_pages"] + data["aio_pages"]]
            )
            for r, name in tracked:
                if (r["prv"]["impressions"] >= MIN_IMP_AIO and
                        r["cur"]["impressions"] >= MIN_IMP_AIO and
                        abs(r["pos_chg"]) >= RANK_CHG_MIN):
                    direction = "改善" if r["pos_chg"] < 0 else "低下"
                    hypos.append(
                        f'「{name}」の順位が {abs(r["pos_chg"]):.1f} 位{direction}している'
                        f'（{r["prv"]["position"]:.0f} 位 → {r["cur"]["position"]:.0f} 位）。'
                    )

        # 地域系KW（imp < MIN_IMP_FINDING）
        weak_region = [
            r for r in data["key_kws"]
            if "大阪" in r["query"] and r["cur"]["impressions"] < MIN_IMP_FINDING
        ]
        if weak_region:
            names = "・".join(f'「{r["query"]}」' for r in weak_region)
            hypos.append(f'{names} は表示回数が少ない状態。')

        if not hypos:
            hypos.append("観察対象となる数値変化は次月以降に整理する。現時点では初期値として記録する。")
        return hypos[:5]

    # ── AIO対策サジェスション ──
    def gen_aio_suggestions() -> list[str]:
        gaio = data.get("google_aio", [])
        ranking, total_aio = aio_competitor_ranking(gaio)
        suggestions = []

        # S1: AIOに表示されているがTCDが未引用のクエリへの対策
        missed = [r["query"] for r in gaio if r.get("aio_exists") and not r.get("tcd_cited") and r.get("query")]
        if missed:
            q_txt = "・".join(f'「{q}」' for q in missed[:3])
            suggestions.append(
                f"{q_txt} でAIOが表示されているがTCDは未引用。"
                "各クエリに対応した「定義→選び方→比較表」形式の長文コンテンツを整備し、引用獲得を狙う。"
            )
        else:
            suggestions.append(
                "観測クエリ全件でAIO引用を獲得中。"
                "引用箇所の質と網羅性を高め、さらに多くのクエリへ展開していく。"
            )

        # S2: 競合ドメインが使うコンテンツ形式への追随
        competitors = [r for r in ranking if not r.get("is_tcd") and r["count"] > 0]
        if competitors:
            top = competitors[0]
            suggestions.append(
                f'{top["name"]} など競合はAIO引用を複数獲得。'
                "同様の「ブランディング会社比較・一覧」型ページを作成し、Googleが一度の検索で解決できるコンテンツを提供する。"
            )
        else:
            suggestions.append(
                "競合のAIO引用実績が少ないうちに先行して「比較・一覧型コンテンツ」を整備し、"
                "AIOの引用元として優位ポジションを確立する。"
            )

        # S3: 構造化データ・技術施策
        suggestions.append(
            "主要サービスページにFAQスキーマ・HowToスキーマなどの構造化データを実装し、"
            "AIへの情報提供を最適化することでAIO引用率の向上を図る。"
        )

        return suggestions

    # ── 次月確認ポイント ──
    def gen_html_checkpoints() -> list[str]:
        checks = []
        for r in sorted(data["key_kws"], key=lambda x: x["cur"]["position"] if x["cur"]["position"] > 0 else 999):
            cd = r["cur"]
            if cd["impressions"] >= 10 and 0 < cd["position"] <= 15:
                checks.append(f'「{r["query"]}」のCTRとSERP表示確認（初期順位 {cd["position"]:.0f} 位）')
                if len(checks) >= 3:
                    break
        osaka = [r for r in data["key_kws"] if "大阪" in r["query"]]
        if osaka:
            names = "・".join(f'「{r["query"]}」' for r in osaka[:2])
            checks.append(f'{names} の表示回数推移を確認')
        svc_def_names = "・".join(r["name"] for r in (svc_active + aio_active)[:2])
        if svc_def_names:
            checks.append(f'{svc_def_names} の露出推移を確認')
        checks.append("Contact / Download の検索流入推移を確認")
        return checks

    # ── 総評（4段落構造） ──
    def summary_html() -> str:
        paras = []

        # P1: 全体状況 + ベースライン注記
        active_areas = []
        if kw_active:
            active_areas.append("ブランディング会社・インナーブランディング領域")
        if aio_active or def_active:
            active_areas.append("サービス・Definition記事群")
        p1 = (
            f'<strong style="color:{NAVY};">&#9432; 初期観測フェーズ</strong> — '
            f'Search Consoleの計測開始が{report_month}中旬のため、本レポートは初期観測フェーズとして扱います。'
        )
        if active_areas:
            p1 += f'ただし、{"および".join(active_areas)}で検索露出が確認できている。'
        paras.append(p1)

        # P2: Definition評価（実数値あり）
        if def_active:
            def _def_label(r: dict) -> str:
                cd = r["cur"]
                s  = f'「{r["name"]}」（{fi(cd["impressions"])} Imp'
                if cd["position"] > 0:
                    s += f'・順位 {cd["position"]:.1f} 位'
                return s + '）'
            names = "・".join(_def_label(r) for r in def_active[:2])
            paras.append(f'Definition群では {names} が先行して反応している。')

        # P3: サービス/AIO評価（実数値あり）
        if aio_active:
            def _aio_label(r: dict) -> str:
                cd = r["cur"]
                s  = f'「{r["name"]}」（{fi(cd["impressions"])} Imp'
                if cd["position"] > 0:
                    s += f'・順位 {cd["position"]:.1f} 位'
                return s + '）'
            names = "・".join(_aio_label(r) for r in aio_active[:2])
            paras.append(f'サービス群では {names} が検索結果に出始めている。')

        # P4: 来月確認
        if kw_active:
            names = "・".join(f'「{r["query"]}」' for r in kw_active[:2])
            paras.append(f'次月以降は {names} 関連KWの推移を重点確認する。')

        return "<br><br>".join(paras)

    period_unit = "週" if period_type == "weekly" else "月"

    kw_name = lambda r: esc(r["query"])

    def page_name(r: dict) -> str:
        title = esc(r.get("title") or r["name"])
        return f'<a href="{esc(r["url"])}" style="color:{NAVY};text-decoration:none;">{title}</a>'

    findings_html      = "".join(finding_row(f)    for f in gen_findings())
    hypothesis_html    = "".join(hypothesis_row(h) for h in gen_hypothesis())
    checkpoints_html   = "".join(check_row(c)      for c in gen_html_checkpoints())
    aio_suggestions_html = "".join(check_row(s)    for s in gen_aio_suggestions())

    # ── Executive Summary（経営層向け要約） ──
    def _executive_summary_section() -> str:
        items = gen_executive_summary(data)
        rows = "".join(
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 6px 6px 0;'
            f'padding:12px 18px;color:{TEXT};font-size:16px;line-height:1.7;">{bold_html(item)}</div>'
            f'</td></tr>'
            for item in items
        )
        return (
            f'<tr><td style="padding-top:4px;padding-bottom:2px;">'
            f'<div style="color:{NAVY};font-size:21px;font-weight:800;margin-bottom:2px;">Executive Summary</div>'
            f'<p style="color:{MUTED};font-size:13px;margin:0 0 12px;">経営層向け要約 — 最初の30秒で状況を把握</p>'
            f'</td></tr>'
            f'{rows}'
        )

    # ── AI Insights セクション（ルールベース仮説） ──
    def _ai_insights_section() -> str:
        rows = "".join(hypothesis_row(i) for i in gen_ai_insights(data))
        return (
            f'<tr><td style="padding-top:28px;padding-bottom:2px;">'
            f'<div style="color:{NAVY};font-size:21px;font-weight:800;margin-bottom:2px;">AI Insights</div>'
            f'<p style="color:{MUTED};font-size:13px;margin:0 0 12px;">Search Console × AIO を横断した変化の背景仮説（ルールベース生成）</p>'
            f'</td></tr>'
            f'{rows}'
        )

    # ── Action Items セクション（来週やること） ──
    def _action_items_section() -> str:
        rows = "".join(check_row(a) for a in gen_action_items(data))
        return (
            f'<tr><td style="padding-top:20px;padding-bottom:2px;">'
            f'<div style="color:{NAVY};font-size:21px;font-weight:800;margin-bottom:2px;">Action Items</div>'
            f'<p style="color:{MUTED};font-size:13px;margin:0 0 12px;">来週やるべき実務タスク（優先度順）</p>'
            f'</td></tr>'
            f'{rows}'
        )

    # ── Google AI Overview セクション（SerpAPI）──
    def _google_aio_section() -> str:
        gaio = data.get("google_aio", [])
        title_html = sec_title("3", "Google AI Overview 観測")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'SerpAPI 経由で Google 検索上の AI Overview（AIによる概要）を観測しています。'
            f'取得失敗はAIOなしとは区別します。'
            f'</p></td></tr>'
        )
        no_real_data = all(r.get("status") == "API_KEY_MISSING" for r in gaio) if gaio else True
        if not gaio or no_real_data:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'観測データなし（SERPAPI_API_KEY 未設定または無効）。</p></td></tr>'
            )

        hdr = (
            th("クエリ", "left", "150") +
            th("AIO有無", "center", "70") +
            th("TCD引用", "center", "70") +
            th("引用URL（上位）") +
            th("競合引用", "left", "120")
        )
        rows_html = ""
        _STATUS_LABEL = {
            "OK_WITH_AIO": ("AIOあり",   UP),
            "OK_NO_AIO":   ("AIOなし",   MUTED),
            "FETCH_ERROR": ("取得失敗",  DOWN),
            "API_KEY_MISSING": ("APIキー未設定", DOWN),
        }
        for r in gaio:
            status = r.get("status", "OK_NO_AIO")
            s_label, s_color = _STATUS_LABEL.get(status, (status, MUTED))

            tcd_cited = r.get("tcd_cited", False)
            tcd_color = UP if tcd_cited else MUTED
            tcd_txt   = "&#9989; 引用あり" if tcd_cited else ("言及のみ" if r.get("tcd_mentioned") else "なし")

            urls = r.get("cited_urls", [])
            url_html = "<br>".join(
                f'<a href="{esc(u)}" style="color:{CYAN};font-size:14px;text-decoration:none;">{esc(_extract_domain(u))}</a>'
                for u in urls[:3]
            ) or f'<span style="color:{MUTED};font-size:14px;">-</span>'

            comp = "・".join(esc(d) for d in r.get("competitor_domains", [])[:3]) or "-"

            rows_html += (
                "<tr>"
                + td_cell(esc(r["query"]))
                + td_cell(f'<span style="color:{s_color};font-weight:700;">{s_label}</span>', "center")
                + td_cell(f'<span style="color:{tcd_color};font-weight:700;">{tcd_txt}</span>', "center")
                + td_cell(url_html)
                + td_cell(f'<span style="font-size:15px;">{comp}</span>')
                + "</tr>\n"
            )
            if tcd_cited and r.get("tcd_cited_urls"):
                tcd_url_html = "・".join(
                    f'<a href="{esc(u)}" style="color:{UP};font-size:14px;">{esc(_extract_domain(u))}</a>'
                    for u in r["tcd_cited_urls"][:2]
                )
                rows_html += (
                    f'<tr style="background:#f0fdf4;"><td colspan="5" '
                    f'style="padding:5px 10px 7px;font-size:14px;color:{UP};">'
                    f'&#9989; TCD引用URL: {tcd_url_html}</td></tr>\n'
                )
            elif r.get("aio_text") and r.get("aio_exists"):
                rows_html += (
                    f'<tr style="background:#f8fafc;"><td colspan="5" '
                    f'style="padding:5px 10px 7px;font-size:14px;color:{MUTED};">'
                    f'&#128203; AIO抜粋: {esc((r["aio_text"] or "")[:120])}…</td></tr>\n'
                )

        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── AIO競合ランキング セクション ──
    def _aio_competitor_section() -> str:
        ranking, total_aio = aio_competitor_ranking(data.get("google_aio", []))
        title_html = sec_title("4", "AIO競合ランキング")
        label = "今週" if period_type == "weekly" else "今月"
        subtitle = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:15px;margin:0 0 8px;">'
            f'{label}引用された企業　（観測AIO: {total_aio}件）</p></td></tr>'
        )
        if not ranking:
            return (
                title_html + subtitle +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'AIOに引用された企業は検出されませんでした。</p></td></tr>'
            )
        hdr = th("企業", "left") + th("対象キーワード", "left") + th("AIシェア", "right", "110")
        rows_html = ""
        for item in ranking:
            kw_txt = "、".join(item.get("keywords", [])) or "—"
            share_txt = f'{item["count"]}件 ({item["share"]:.0f}%)'
            is_tcd = item.get("is_tcd", False)
            name_cell = (
                f'<span style="color:{NAVY};font-weight:700;">{esc(item["name"])}</span>'
                if is_tcd else esc(item["name"])
            )
            share_color = UP if item["count"] > 0 else MUTED
            share_cell = f'<span style="color:{share_color};font-weight:700;">{share_txt}</span>'
            row_style = ' style="background:#f0fdf4;"' if is_tcd else ""
            rows_html += f'<tr{row_style}>{td_cell(name_cell)}{td_cell(esc(kw_txt))}{td_cell(share_cell, "right")}</tr>\n'
        table_html = wrap_table(hdr, rows_html)

        hypothesis = gen_aio_competitor_hypothesis(ranking, total_aio, label)
        hypo_html = ""
        if hypothesis:
            hypo_html = (
                f'<tr><td style="padding:10px 0 16px;">'
                f'<p style="color:{TEXT};font-size:15px;line-height:1.8;margin:0;">{esc(hypothesis)}</p>'
                f'</td></tr>'
            )

        return title_html + subtitle + f'<tr><td style="padding-bottom:8px;">{table_html}</td></tr>' + hypo_html

    # ── 定点SERP観測 セクション（重点4キーワード） ──
    def _point_watch_section() -> str:
        pw = data.get("point_watch", [])
        title_html = sec_title("5", "定点SERP観測（重点4キーワード）")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'「ブランディング会社 東京/大阪」「インナーブランディング会社 東京/大阪」の4キーワードを毎週定点観測し、'
            f'AIOの有無・引用元の変化を追跡しています。'
            f'</p></td></tr>'
        )
        if not pw:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'観測データなし（SERPAPI_API_KEY 未設定、または point_watch_queries 未設定）。</p></td></tr>'
            )
        rate = point_watch_aio_presence_rate(pw)
        rate_row = ""
        if rate is not None:
            rate_row = (
                f'<tr><td style="padding-bottom:10px;">'
                f'<span style="color:{NAVY};font-size:15px;font-weight:700;">AIO表示率（定点観測4キーワード中）: {rate:.0f}%</span>'
                f'</td></tr>'
            )
        hdr = (
            th("キーワード", "left", "140") +
            th("AIO有無", "center", "60") +
            th("AIO変化", "center", "80") +
            th("引用元変化", "center", "80") +
            th("SERP安定度", "center", "80") +
            th("新規引用ドメイン（誰が増えたか）") +
            th("消失ドメイン")
        )
        rows_html = ""
        _STABILITY_COLOR = {"変化なし": MUTED, "軽微": "#e0a930", "大きい": DOWN, "初回観測": MUTED}
        for r in pw:
            aio_color = UP if r["aio_exists"] else MUTED
            aio_txt   = "あり" if r["aio_exists"] else "なし"
            chg_color = UP if r["aio_change"] not in ("変化なし",) else MUTED
            if not r["has_previous"]:
                cite_txt, cite_color = "初回観測", MUTED
            elif r["citation_changed"]:
                cite_txt, cite_color = "変化あり", DOWN
            else:
                cite_txt, cite_color = "変化なし", MUTED
            stability_color = _STABILITY_COLOR.get(r["serp_stability"], MUTED)
            new_d  = "・".join(esc(d) for d in r["new_domains"]) or "-"
            lost_d = "・".join(esc(d) for d in r["lost_domains"]) or "-"
            rows_html += (
                "<tr>"
                + td_cell(esc(r["query"]))
                + td_cell(f'<span style="color:{aio_color};font-weight:700;">{aio_txt}</span>', "center")
                + td_cell(f'<span style="color:{chg_color};">{esc(r["aio_change"])}</span>', "center")
                + td_cell(f'<span style="color:{cite_color};font-weight:700;">{cite_txt}</span>', "center")
                + td_cell(f'<span style="color:{stability_color};font-weight:700;">{esc(r["serp_stability"])}</span>', "center")
                + td_cell(f'<span style="font-size:14px;">{new_d}</span>')
                + td_cell(f'<span style="font-size:14px;color:{MUTED};">{lost_d}</span>')
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + rate_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── AIO Opportunity セクション ──
    def _aio_opportunity_section() -> str:
        opp = data.get("aio_opportunity", [])
        title_html = sec_title("6", "AIO Opportunity")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'AIOに引用される可能性がありながら未引用のキーワード（Imp 100以上・順位15位以内・TCD未引用）を抽出しています。'
            f'</p></td></tr>'
        )
        if not opp:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'該当するキーワードは検出されませんでした。</p></td></tr>'
            )
        hdr = (
            th("キーワード", "left", "140") +
            th("IMP", "right", "60") + th("Click", "right", "55") +
            th("CTR", "right", "55") + th("順位", "right", "55") +
            th("AIO表示", "center", "70") +
            th("競合引用ドメイン") + th("推奨アクション")
        )
        rows_html = ""
        for o in opp:
            aio_color = UP if o["aio_present"] else MUTED
            aio_txt   = "あり" if o["aio_present"] else "なし"
            comp = "・".join(esc(d) for d in o["competitor_domains"]) or "-"
            rows_html += (
                "<tr>"
                + td_cell(esc(o["keyword"]))
                + td_cell(fi(o["impressions"]), "right")
                + td_cell(fi(o["clicks"]), "right")
                + td_cell(fp(o["ctr"]), "right")
                + td_cell(fpos(o["position"]), "right")
                + td_cell(f'<span style="color:{aio_color};font-weight:700;">{aio_txt}</span>', "center")
                + td_cell(f'<span style="font-size:14px;">{comp}</span>')
                + td_cell(f'<span style="font-size:14px;">{esc(o["suggested_action"])}</span>')
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── AIO Daily Summary セクション（aio_daily_monitor.py 連携） ──
    def _aio_daily_summary_section() -> str:
        summary = data.get("aio_daily_summary", {})
        title_html = sec_title("7", "AIO Daily Summary")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'aio_daily_monitor.py（毎朝6時実行）が観測した日次AIOデータを、本レポートと同じ対象期間で集計しています。'
            f'</p></td></tr>'
        )
        if not summary or summary.get("aio_query_count", 0) == 0:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'AIO日次データ未取得（data/aio/looker/aio_daily.csv が存在しないか、対象期間のデータがありません）。</p></td></tr>'
            )
        rate     = summary["aio_exists_rate"]
        tcd_rate = summary["tcd_cited_rate"]
        stat_cells = [
            ("観測クエリ数",      str(summary["aio_query_count"])),
            ("AIO表示率",        f'{rate*100:.0f}%' if rate is not None else "-"),
            ("TCD引用率",        f'{tcd_rate*100:.0f}%' if tcd_rate is not None else "-"),
            ("AIO本文変更件数",   str(summary["aio_text_changed_count"])),
        ]
        if summary.get("error_count"):
            stat_cells.append(("エラー件数", str(summary["error_count"])))
        cells_html = "".join(
            f'<td style="padding:10px 16px;text-align:center;border-right:1px solid {BORDER};">'
            f'<div style="color:{MUTED};font-size:13px;margin-bottom:4px;">{esc(label)}</div>'
            f'<div style="color:{NAVY};font-size:20px;font-weight:700;">{esc(val)}</div></td>'
            for label, val in stat_cells
        )
        stat_table = (
            f'<table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;border:1px solid {BORDER};border-radius:6px;overflow:hidden;">'
            f'<tr>{cells_html}</tr></table>'
        )
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{stat_table}</td></tr>'

    # ── クエリ別AIO日次モニター セクション ──
    def _aio_query_daily_section() -> str:
        query_level = data.get("aio_daily_summary", {}).get("query_level", [])
        title_html = sec_title("8", "クエリ別AIO日次モニター")
        if not query_level:
            return (
                title_html +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'AIO日次データ未取得。</p></td></tr>'
            )
        hdr = (
            th("クエリ", "left", "160") +
            th("AIO表示日数", "right", "90") +
            th("AIO表示率", "right", "80") +
            th("TCD引用日数", "right", "90") +
            th("TCD言及日数", "right", "90") +
            th("最新ステータス", "center", "100") +
            th("本文変更回数", "right", "90")
        )
        _STATUS_LABEL = {
            "OK_WITH_AIO": ("AIOあり", UP), "OK_NO_AIO": ("AIOなし", MUTED),
            "FETCH_ERROR": ("取得失敗", DOWN), "API_KEY_MISSING": ("APIキー未設定", DOWN),
        }
        rows_html = ""
        for r in query_level:
            rate = f"{r['aio_exists_rate']*100:.0f}%" if r["aio_exists_rate"] is not None else "-"
            s_label, s_color = _STATUS_LABEL.get(r["latest_status"], (r["latest_status"], MUTED))
            rows_html += (
                "<tr>"
                + td_cell(esc(r["query"]))
                + td_cell(str(r["aio_exists_days"]), "right")
                + td_cell(rate, "right")
                + td_cell(str(r["tcd_cited_days"]), "right")
                + td_cell(str(r["tcd_mentioned_days"]), "right")
                + td_cell(f'<span style="color:{s_color};font-weight:700;">{s_label}</span>', "center")
                + td_cell(str(r["aio_text_changed_count"]), "right")
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── 引用ドメイン推移 セクション ──
    def _aio_domain_trend_section() -> str:
        trend = data.get("aio_domain_trend", {})
        rows = trend.get("top_domains", []) + trend.get("lost_rows", [])
        title_html = sec_title("9", "引用ドメイン推移")
        if not rows:
            return (
                title_html +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'AIO引用ドメインデータ未取得。</p></td></tr>'
            )
        hdr = (
            th("ドメイン", "left", "160") +
            th("引用回数", "right", "70") +
            th("対象クエリ") +
            th("初出日", "center", "90") +
            th("最終観測日", "center", "90") +
            th("状態", "center", "70")
        )
        _STATUS_LABEL = {"new": ("🆕 新規", UP), "lost": ("⚠️ 消失", DOWN), "": ("-", MUTED)}
        rows_html = ""
        for r in rows:
            s_label, s_color = _STATUS_LABEL.get(r["status"], (r["status"], MUTED))
            queries = "・".join(esc(q) for q in r["related_queries"][:3]) or "-"
            row_style = ' style="background:#fef2f2;"' if r["status"] == "lost" else ""
            rows_html += (
                f"<tr{row_style}>"
                + td_cell(esc(r["domain"]))
                + td_cell(str(r["cited_count"]), "right")
                + td_cell(f'<span style="font-size:14px;">{queries}</span>')
                + td_cell(esc(r["first_seen"]), "center")
                + td_cell(esc(r["last_seen"]), "center")
                + td_cell(f'<span style="color:{s_color};font-weight:700;">{s_label}</span>', "center")
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── AIO本文変化サマリー セクション ──
    def _aio_text_change_section() -> str:
        changes = data.get("aio_text_changes", {})
        title_html = sec_title("10", "AIO本文変化サマリー")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'本文全文は data/aio/daily/YYYY-MM-DD.json を参照してください。ここではハッシュ変化のみを扱います。'
            f'</p></td></tr>'
        )
        if not changes or changes.get("changed_count", 0) == 0:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'対象期間中、AIO本文の変化は検出されませんでした。</p></td></tr>'
            )
        count_row = (
            f'<tr><td style="padding-bottom:10px;">'
            f'<span style="color:{NAVY};font-size:15px;font-weight:700;">本文変更件数: {changes["changed_count"]}件</span>'
            f'</td></tr>'
        )
        items_html = "".join(
            f'<tr><td style="padding-bottom:4px;">'
            f'<div style="padding:4px 0 4px 12px;border-left:3px solid {BORDER};color:{TEXT};font-size:15px;">'
            f'{esc(c["date"])}: 「{esc(c["query"])}」</div></td></tr>'
            for c in changes["changed_queries"]
        )
        return title_html + note_row + count_row + items_html + f'<tr><td style="padding-bottom:12px;"></td></tr>'

    # ── 新規・更新ページのインデックス監視 セクション ──
    def _index_monitoring_section() -> str:
        idx_mon = data.get("index_monitoring", [])
        title_html = sec_title("16", "新規・更新ページのインデックス監視")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'新規Definition記事・更新ページがSearch Console上に出始めたかを追跡しています'
            f'（config/aio_monitor.yml の new_index_watch に追加すると対象になります）。'
            f'</p></td></tr>'
        )
        if not idx_mon:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'監視対象ページが未登録です。</p></td></tr>'
            )
        _STATUS_LABEL_HTML = {
            "not_seen":              ("未検出", MUTED),
            "indexed_no_impression": ("インデックス済・表示回数0", MUTED),
            "impression_started":    ("表示開始", "#e0a930"),
            "ranking_started":       ("クリック発生", UP),
        }
        hdr = (
            th("ページ", "left", "180") +
            th("公開・更新日", "center", "100") +
            th("初回表示検出", "center", "100") +
            th("IMP", "right", "60") + th("Click", "right", "55") +
            th("平均順位", "right", "65") +
            th("ステータス", "center", "110")
        )
        rows_html = ""
        for p in idx_mon:
            s_label, s_color = _STATUS_LABEL_HTML.get(p["status"], (p["status"], MUTED))
            first_seen = esc(p["first_seen_in_search_console"] or "-")
            rows_html += (
                "<tr>"
                + td_cell(f'<a href="{esc(p["url"])}" style="color:{NAVY};text-decoration:none;">{esc(p["page_title"])}</a>')
                + td_cell(esc(p["published_or_updated_date"] or "-"), "center")
                + td_cell(first_seen, "center")
                + td_cell(fi(p["impressions"]), "right")
                + td_cell(fi(p["clicks"]), "right")
                + td_cell(fpos(p["average_position"]), "right")
                + td_cell(f'<span style="color:{s_color};font-weight:700;">{s_label}</span>', "center")
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    executive_summary_html = _executive_summary_section()
    ai_insights_html = _ai_insights_section()
    action_items_html = _action_items_section()
    google_aio_html = _google_aio_section()
    aio_competitor_html = _aio_competitor_section()
    point_watch_html = _point_watch_section()
    aio_opportunity_html = _aio_opportunity_section()
    aio_daily_summary_html = _aio_daily_summary_section()
    aio_query_daily_html = _aio_query_daily_section()
    aio_domain_trend_html = _aio_domain_trend_section()
    aio_text_change_html = _aio_text_change_section()
    index_monitoring_html = _index_monitoring_section()

    kw_table_html  = two_row_table(kw_active,  kw_name)
    svc_table_html = two_row_table(svc_active, page_name)
    aio_table_html = two_row_table(aio_active, page_name)
    def_table_html = two_row_table(def_active, page_name)
    lp_table_html  = two_row_table(lp_active,  page_name)

    kw_note  = hidden_note(kw_hidden,  len(data["key_kws"]))
    svc_note = hidden_note(svc_hidden, len(data["service_pages"]))
    aio_note = hidden_note(aio_hidden, len(data["aio_pages"]))
    def_note = hidden_note(def_hidden, len(data["def_pages"]))
    lp_note  = hidden_note(lp_hidden,  len(data["lp_pages"]))

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{report_title}</title>
</head>
<body style="margin:0;padding:0;background:{BG};color:{TEXT};font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:32px 12px;">
<tr><td align="center">
<table width="850" cellpadding="0" cellspacing="0" style="max-width:850px;width:100%;background:#fff;border-radius:10px;border:1px solid {BORDER};overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,.07);">

<!-- HEADER -->
<tr><td style="background:{NAVY};padding:36px 40px;">
  <div style="color:{CYAN};font-size:13px;font-weight:700;letter-spacing:3px;text-transform:uppercase;margin-bottom:14px;">{header_label}</div>
  <h1 style="color:#fff;font-size:26px;font-weight:700;margin:0 0 18px;line-height:1.35;letter-spacing:-.3px;">{report_title}</h1>
  <table cellpadding="0" cellspacing="0"><tr>
    <td style="padding-right:28px;">
      <div style="color:{CYAN};font-size:13px;font-weight:700;letter-spacing:1px;margin-bottom:4px;">{cur_label}</div>
      <div style="color:#cbd5e1;font-size:15px;">{cs} 〜 {ce}</div>
    </td>
    <td>
      <div style="color:#64748b;font-size:13px;font-weight:700;letter-spacing:1px;margin-bottom:4px;">{prv_label}</div>
      <div style="color:#64748b;font-size:15px;">{ps} 〜 {pe}</div>
    </td>
  </tr></table>
</td></tr>

<!-- BODY -->
<tr><td style="padding:8px 40px 40px;">
<table width="100%" cellpadding="0" cellspacing="0">

<!-- Executive Summary -->
{executive_summary_html}

<!-- AI Insights -->
{ai_insights_html}

<!-- Action Items -->
{action_items_html}

<!-- 1. 総評 -->
<tr><td style="padding-top:32px;padding-bottom:4px;">
  <div style="color:{NAVY};font-size:19px;font-weight:700;margin-bottom:10px;">1. 総評</div>
</td></tr>
{info_box(summary_html())}

<!-- 2. 今週/月の発見 -->
{sec_title("2", f"今{period_unit}の発見")}
{findings_html}

<!-- 3. Google AI Overview 観測 -->
{google_aio_html}

<!-- 4. AIO競合ランキング -->
{aio_competitor_html}

<!-- 5. 定点SERP観測（重点4キーワード） -->
{point_watch_html}

<!-- 6. AIO Opportunity -->
{aio_opportunity_html}

<!-- 7. AIO Daily Summary -->
{aio_daily_summary_html}

<!-- 8. クエリ別AIO日次モニター -->
{aio_query_daily_html}

<!-- 9. 引用ドメイン推移 -->
{aio_domain_trend_html}

<!-- 10. AIO本文変化サマリー -->
{aio_text_change_html}

<!-- 11. 重点キーワード監視 -->
{sec_title("11", "重点キーワード監視")}
<tr><td style="padding-bottom:4px;">{kw_table_html}</td></tr>
{kw_note}

<!-- 12. サービス系 -->
{sec_title("12", "サービス系")}
<tr><td style="padding-bottom:4px;">{svc_table_html}</td></tr>
{svc_note}

<!-- 13. サービスDefinition系 -->
{sec_title("13", "サービスDefinition系")}
<tr><td style="padding-bottom:4px;">{aio_table_html}</td></tr>
{aio_note}

<!-- 14. マガジン系 -->
{sec_title("14", "マガジン系")}
<tr><td style="padding-bottom:4px;">{def_table_html}</td></tr>
{def_note}

<!-- 15. 重要ページ監視 -->
{sec_title("15", "重要ページ監視")}
<tr><td style="padding-bottom:4px;"><p style="color:{MUTED};font-size:14px;margin:0 0 8px 0;">Search Console上の検索流入評価。実際のCV数ではありません（CV計測は将来GA4で実施）。</p></td></tr>
<tr><td style="padding-bottom:4px;">{lp_table_html}</td></tr>
{lp_note}

<!-- 16. 新規・更新ページのインデックス監視 -->
{index_monitoring_html}

<!-- 17. 今週/月の仮説 -->
{sec_title("17", f"今{period_unit}の仮説")}
{hypothesis_html}

<!-- 18. AIO対策サジェスション -->
{sec_title("18", "AIO対策サジェスション")}
{aio_suggestions_html}

<!-- 19. 次週/月確認ポイント -->
{sec_title("19", f"次{period_unit}確認ポイント")}
{checkpoints_html}

</table>
</td></tr>

<!-- Data Studio リンク -->
<tr><td style="padding:40px 40px;text-align:center;border-top:1px solid {BORDER};">
  <a href="https://datastudio.google.com/reporting/4d67c760-d3dc-4a1e-b2d2-47ce3b44f69a"
     style="display:inline-block;padding:18px 24px;background:{NAVY};color:#fff;
            font-size:21px;font-weight:600;text-decoration:none;border-radius:5px;letter-spacing:.3px;">
    Data Studio で確認する →
  </a>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:{BG};border-top:1px solid {BORDER};padding:18px 40px;text-align:center;">
  <p style="color:{MUTED};font-size:14px;margin:0;line-height:1.7;">
    Generated by TCD SEO / AIO Monitor<br>生成日時: {generated_at}
  </p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ────────────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TCD SEO/AIO 月次レポート生成スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config/aio_monitor.yml", help="設定ファイルパス")
    parser.add_argument(
        "--credentials-file",
        default="credentials.json",
        help="OAuth2 クライアント認証 JSON ファイルパス（デフォルト: credentials.json）",
    )
    parser.add_argument("--output-dir", default="reports", help="レポート出力先ディレクトリ")
    parser.add_argument("--no-html", action="store_true", help="HTML 出力をスキップ")
    parser.add_argument("--send-email", action="store_true", help="Gmail API でレポートをメール送信する")
    parser.add_argument("--force-aio", action="store_true", help="AIO観測キャッシュを無視して再取得する")
    parser.add_argument("--to", default="kawauchi@tcd.jp", help="送信先メールアドレス（デフォルト: kawauchi@tcd.jp）")
    parser.add_argument(
        "--period",
        choices=["monthly", "weekly", "auto"],
        default="monthly",
        help="レポート期間種別（auto: 月初月曜→monthly / それ以外の月曜→weekly）",
    )
    args = parser.parse_args()

    # 設定読み込み
    config = load_config(args.config)
    site_url: str = config["site_url"]
    top_n: int = config.get("report", {}).get("top_n", 20)
    aio_min_impr: int = config.get("report", {}).get("aio_min_impressions", 100)

    # 認証
    print("Search Console API に接続中...")
    service = get_service(args.credentials_file)

    # 期間確定（auto は内部で monthly / weekly に解決）
    today = datetime.today()
    if args.period == "auto":
        actual_period = "monthly" if today.weekday() == 0 and today.day <= 7 else "weekly"
        print(f"  period=auto → {actual_period} として実行")
    else:
        actual_period = args.period

    cur_start, cur_end, prv_start, prv_end = get_periods(actual_period)
    if actual_period == "weekly":
        print(f"  対象週:   {cur_start} 〜 {cur_end}（月〜日）")
        print(f"  比較週:   {prv_start} 〜 {prv_end}（月〜日）")
    else:
        print(f"  前月:   {cur_start} 〜 {cur_end}")
        print(f"  前々月: {prv_start} 〜 {prv_end}")

    # データ取得
    print("データ取得中...")
    print("  クエリデータ（今期）...")
    cur_queries = fetch_queries(service, site_url, cur_start, cur_end)
    print(f"    {len(cur_queries)} 行取得")
    print("  クエリデータ（前期）...")
    prv_queries = fetch_queries(service, site_url, prv_start, prv_end)
    print(f"    {len(prv_queries)} 行取得")
    print("  ページデータ（今期）...")
    cur_pages = fetch_pages(service, site_url, cur_start, cur_end)
    print(f"    {len(cur_pages)} 行取得")
    print("  ページデータ（前期）...")
    prv_pages = fetch_pages(service, site_url, prv_start, prv_end)
    print(f"    {len(prv_pages)} 行取得")
    print("  デバイスデータ...")
    cur_devices = fetch_devices(service, site_url, cur_start, cur_end)

    # Watching.csv 読み込み
    watching = load_watching_csv(config.get("watching_csv", "Watching.csv"))

    # Google AI Overview 観測
    aio_cfg = config.get("google_aio", {})
    gaio_output_dir = str(Path(args.output_dir) / "google_aio")
    if aio_cfg.get("enabled", False):
        print("Google AI Overview を観測中（SerpAPI）...")
        today_str = today.strftime("%Y-%m-%d")
        gaio_previous = load_previous_gaio_snapshot(gaio_output_dir, exclude_date=today_str)
        google_aio_data = fetch_google_aio(
            queries        = aio_cfg.get("queries", []),
            aio_cfg        = aio_cfg,
            output_dir     = gaio_output_dir,
            force          = args.force_aio,
            domain_aliases = config.get("domain_aliases", {}),
        )
        point_watch_data = compare_point_watch(
            google_aio_data, gaio_previous, config.get("point_watch_queries", []),
            domain_aliases=config.get("domain_aliases", {}),
        )
    else:
        google_aio_data = []
        point_watch_data = []

    # AIO日次モニター（aio_daily_monitor.py）データ読み込み
    print("AIO日次モニターデータ読み込み中...")
    aio_daily_df = load_aio_daily_csv()
    aio_domains_df = load_aio_domains_daily_csv()
    aio_daily_summary = aggregate_aio_daily(aio_daily_df, cur_start, cur_end)
    aio_domain_trend = aggregate_aio_domain_trend(aio_domains_df, cur_start, cur_end, prv_start, prv_end)
    aio_text_changes = aggregate_aio_text_changes(aio_daily_df, cur_start, cur_end)
    print(f"  集計対象期間: {cur_start} 〜 {cur_end}")
    print(f"  AIO表示件数: {aio_daily_summary['aio_exists_count']} / TCD引用件数: {aio_daily_summary['tcd_cited_count']}")

    # 集計・分析
    print("データ集計中...")
    data = {
        "periods": ((cur_start, cur_end), (prv_start, prv_end)),
        "totals": {
            "current": calc_totals(cur_pages),
            "previous": calc_totals(prv_pages),
        },
        "key_kws": kw_compare(cur_queries, prv_queries, config.get("key_keywords", [])),
        "service_pages": page_compare(cur_pages, prv_pages, watching.get("サービス系", []), site_url),
        "aio_pages":     page_compare(cur_pages, prv_pages, watching.get("サービスDefinition系", []), site_url),
        "def_pages":     page_compare(cur_pages, prv_pages, watching.get("マガジン系", []), site_url),
        "lp_pages":      page_compare(cur_pages, prv_pages, config.get("lp_cv_pages", []), site_url),
        "devices": cur_devices.to_dict("records") if not cur_devices.empty else [],
        "aio_anomalies": detect_aio_anomalies(cur_queries, min_impr=aio_min_impr),
        "top_queries":    top_queries_cmp(cur_queries, prv_queries, n=top_n),
        "top_pages":      top_pages_cmp(cur_pages, prv_pages, n=top_n),
        "google_aio":       google_aio_data,
        "point_watch":      point_watch_data,
        "aio_opportunity":  compute_aio_opportunity(google_aio_data, cur_queries),
        "index_monitoring": compute_index_monitoring(
            config.get("new_index_watch", []) or [], cur_pages, prv_pages, site_url
        ),
        "aio_daily_summary": aio_daily_summary,
        "aio_domain_trend":  aio_domain_trend,
        "aio_text_changes":  aio_text_changes,
        "period_type":    actual_period,
    }

    # レポート生成
    print("レポート生成中...")
    md_content = gen_markdown(data, config)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if actual_period == "weekly":
        base_name = f"{cur_end}-weekly-seo-aio-report"
    else:
        base_name = f"{cur_start[:7]}-seo-aio-report"

    md_path = output_dir / f"{base_name}.md"
    md_path.write_text(md_content, encoding="utf-8")
    print(f"  Markdown: {md_path}")

    html_content = None
    if not args.no_html or args.send_email:
        html_content = gen_html(data, config)

    if not args.no_html and html_content:
        html_path = output_dir / f"{base_name}.html"
        html_path.write_text(html_content, encoding="utf-8")
        print(f"  HTML:     {html_path}")

    if args.send_email and html_content:
        if actual_period == "weekly":
            subject = f"TCD SEO/AIO 週次レポート {cur_start}〜{cur_end}"
        else:
            month_str = datetime.strptime(cur_start, "%Y-%m-%d").strftime("%Y年%m月")
            subject = f"TCD SEO/AIO 月次レポート {month_str}"
        print(f"メール送信中 → {args.to} ...")
        gmail_svc = get_gmail_service(args.credentials_file)
        send_html_email(gmail_svc, args.to, subject, html_content)
        print(f"  送信完了!")

    print("\n完了!")


if __name__ == "__main__":
    main()
