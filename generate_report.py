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

import page_query_analysis as pqa


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

def fetch_page_first_dates(svc, site: str, full_url: str, start_date: str, end_date: str) -> tuple[str | None, str | None]:
    """指定ページの日次データから、初回Impression日・初回Click日を取得する。"""
    rows = _fetch_rows(
        svc, site, start_date, end_date, ["date"],
        dimension_filter={"dimension": "page", "operator": "equals", "expression": full_url},
    )
    df = _to_df(rows, ["date"])
    if df.empty:
        return None, None
    df = df.sort_values("date")
    imp_rows = df[df["impressions"] > 0]
    click_rows = df[df["clicks"] > 0]
    first_impression = imp_rows.iloc[0]["date"] if not imp_rows.empty else None
    first_click = click_rows.iloc[0]["date"] if not click_rows.empty else None
    return first_impression, first_click

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


def _root_domain(hostname: str) -> str:
    """www. サブドメインをルートドメインへ正規化する。"""
    h = (hostname or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


_DOMAIN_LIKE_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)


def _looks_like_domain(s: str) -> bool:
    """'www.example.com' のようなホスト名らしい文字列か判定する（会社名の誤正規化を防ぐ）。"""
    return bool(_DOMAIN_LIKE_RE.match((s or "").strip()))


def is_google_domain(domain: str) -> bool:
    """Google検索・Google自身のURL（support.google.com, maps.google.co.jpなど）かどうかを判定する。"""
    d = (domain or "").lower()
    return d == "google" or "google." in d


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


def normalize_domain_strict(raw: str, alias_lookup: dict[str, str]) -> str:
    """alias_lookup に一致した場合のみドメインを返す。一致しなければ空文字を返す
    （会社名をそのままdomain列へ代入しないため。URLが取得できない参照に使う）。"""
    if not raw:
        return ""
    return alias_lookup.get(_normalize_query(raw), "")


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
                 "cited_urls": [], "cited_domain_urls": [], "cited_domains": [], "competitor_domains": [],
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
        cited_domain_urls: list[str] = []
        cited_domains:list[str] = []
        cited_display_names: list[str] = []
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
                        url, src_name, title = ref, "", ""
                    else:
                        # SerpAPIのai_overview.references[]は "link"キー（"url"ではない）。
                        # sourceが"Google"の参照はGoogle内部のビューアリンクであり実際の外部URLではないため、
                        # cited_urls等に混入させない（外部ドメインではないのでスクレイピング対象にもならない）。
                        raw_link = ref.get("link", "") or ref.get("url", "")
                        url      = "" if raw_link.startswith("https://www.google.com/") else raw_link
                        src      = ref.get("source", {})
                        src_name = (src.get("name", "") if isinstance(src, dict) else str(src))
                        title    = ref.get("title", "")
                    # ドメイン（正規化キー）は必ずURLのホスト名から求め、
                    # 表示名（会社名など）と混同しない。www.はルートドメインへ正規化する。
                    # URLが取得できない参照は、config.domain_aliasesに登録済みの会社名のみ
                    # ドメインへ解決する。未登録の会社名はdomainを空欄のままにし、
                    # 会社名をdomain列へ代入しない（表示はdisplay_nameで行う）。
                    url_domain = _root_domain(_extract_domain(url)) if url else ""
                    fallback = _root_domain(src_name) if _looks_like_domain(src_name) else src_name
                    domain = (
                        normalize_domain(url_domain, alias_lookup) if url_domain
                        else normalize_domain_strict(fallback, alias_lookup)
                    )
                    display_name = src_name or domain or url or "unknown"

                    # cited_domains / cited_display_names / cited_domain_urls は常に同じ添字で対応させる
                    # （domainが空欄でもエンティティを区別できるようにするため）。
                    # cited_urls は表示専用の非空URLのみのリスト（添字は対応しない）。
                    if url: cited_urls.append(url)
                    cited_domains.append(domain)
                    cited_domain_urls.append(url)
                    cited_display_names.append(display_name)
                    if title: cited_titles.append(title)

                    chk = (url + " " + domain + " " + src_name + " " + title).lower()
                    if any(p.lower() in chk for p in _TCD_PATTERNS):
                        tcd_cited = True
                        tcd_cited_urls.append(url)

                check_text = (aio_text or "").lower()
                tcd_mentioned = any(p.lower() in check_text for p in _TCD_PATTERNS) or tcd_cited
                competitor_domains = [d for d in cited_domains
                                      if d and not any(p.lower() in d.lower() for p in _TCD_PATTERNS)
                                      and not is_google_domain(d)][:5]

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
            "cited_domain_urls":  cited_domain_urls,
            "cited_domains":      cited_domains,
            "cited_display_names": cited_display_names,
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
    "aio_text_hash", "aio_text_changed", "observation_type", "previous_hash_exists", "status",
]
_AIO_DOMAINS_COLUMNS = ["date", "query", "cited_domain", "cited_domain_count"]
_AIO_DAILY_BOOL_COLUMNS = ["aio_exists", "tcd_cited", "tcd_mentioned", "aio_text_changed", "previous_hash_exists"]


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


AIO_DAILY_MIN_REFERENCE_DAYS = 3   # これ未満は「参考値」表示
AIO_DAILY_MIN_COMPARISON_DAYS = 7  # これ未満は週次比較を行わない


def aggregate_aio_daily(df: pd.DataFrame, start: str, end: str) -> dict:
    """aio_daily.csv を、週次/月次レポートと同じ対象期間で集計する。

    observed_days（実際に観測できた日数）が expected_days（対象期間の日数）に
    満たない場合、AIO表示率・引用率は「参考値」として扱う（observed_daysが
    AIO_DAILY_MIN_REFERENCE_DAYS未満の場合は特に強く警告する）。
    """
    expected_days = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
    empty = {
        "aio_query_count": 0, "aio_exists_count": 0, "aio_exists_rate": None,
        "tcd_cited_count": 0, "tcd_cited_rate": None, "tcd_mentioned_count": 0,
        "aio_text_changed_count": 0, "error_count": 0, "query_level": [],
        "observed_days": 0, "expected_days": expected_days, "coverage_rate": 0.0,
        "is_reference_only": True, "supports_weekly_comparison": False,
    }
    if df.empty or "date" not in df.columns:
        return empty

    period = df[(df["date"] >= start) & (df["date"] <= end)]
    if period.empty:
        return empty

    n_rows               = len(period)
    observed_days         = period["date"].nunique()
    coverage_rate          = observed_days / expected_days if expected_days else 0.0
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
            "observed_days":         n_days,
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
        "observed_days":          observed_days,
        "expected_days":          expected_days,
        "coverage_rate":          coverage_rate,
        "is_reference_only":      observed_days < AIO_DAILY_MIN_REFERENCE_DAYS,
        "supports_weekly_comparison": observed_days >= AIO_DAILY_MIN_COMPARISON_DAYS,
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


_ENTITY_TYPE_LABEL = {
    "branding_company": "ブランディング会社",
    "consulting_company": "コンサル会社",
    "client_company": "クライアント企業",
    "media": "メディア",
    "public_institution": "行政機関",
    "financial_institution": "金融機関",
    "platform": "プラットフォーム",
    "unknown": "分類未登録",
}


def build_citation_trends(trend: dict, domain_entity_types: dict, weekly_snapshot_ranking: list[dict] | None = None) -> dict:
    """引用ドメインの一覧表示ではなく、「市場で誰の存在感が変化したか」を示す形に組み替える。
    ドメインの一覧・引用回数はaggregate_aio_domain_trend()（日次AIO推移データ）の集計結果をそのまま使い、
    ページ本文の内容分類（比較記事／定義記事等）は行わない（スクレイピングを伴わないため）。
    日次AIO推移データが今週分未取得の場合は、継続/新規/消失の判定を空欄で3つ並べるのではなく、
    週次AIOスナップショット（weekly_snapshot_ranking）から分かる範囲の参考情報に差し替える。"""
    top_domains = (trend or {}).get("top_domains", [])
    lost_rows = (trend or {}).get("lost_rows", [])
    stable = [r for r in top_domains if r["status"] != "new"]
    emerging = [r for r in top_domains if r["status"] == "new"]

    # top_domainsが空 かつ lost_rowsが非空 は「今週分の日次AIO推移データが存在しない」ことを意味する
    # （aggregate_aio_domain_trendは今週分の行が1件もなければtop_domainsを作れない）。
    # この場合、前週の引用が実際に消失したのではなく単に観測できていないだけなので、
    # 「消失」と断定せず判断保留とし、disappeared_playersも誤解を招くため空にする。
    no_current_observation = not top_domains and bool(lost_rows)
    disappeared = [] if no_current_observation else lost_rows

    lines: list[str] = []
    weekly_snapshot_note = None
    if no_current_observation:
        lines.append(
            f"今週分の日次AIO推移データが未取得のため、継続・新規・消失の前週比較は判断保留です"
            f"（前週は{len(lost_rows)}社の引用を観測済み）。"
        )
        competitors = [r for r in (weekly_snapshot_ranking or []) if not r.get("is_tcd") and r.get("count", 0) > 0]
        top = sorted(competitors, key=lambda r: -r["count"])[:3]
        if top:
            names = "と".join(r["display_name"] for r in top)
            weekly_snapshot_note = f"週次AIOスナップショット上では、{names}が複数市場で引用されています。"
    elif not top_domains and not disappeared:
        lines.append("対象期間中、AIOでの引用ドメインは観測されませんでした。")
    else:
        if stable:
            names = "・".join(r["domain"] for r in stable[:3])
            lines.append(f"継続的に引用されているのは{names}など{len(stable)}社で、市場での存在感を維持している。")
        if emerging:
            names = "・".join(r["domain"] for r in emerging[:3])
            lines.append(f"今週新たに{names}など{len(emerging)}社が引用され、存在感を高めた可能性がある。")
        if disappeared:
            names = "・".join(r["domain"] for r in disappeared[:3])
            lines.append(f"前週まで引用されていた{names}など{len(disappeared)}社は、今週は引用が確認できなかった。")
        if not emerging and not disappeared:
            lines.append("新規引用・引用消失はなく、引用元の構成は前週から安定している。")

    # エンティティ種別の偏り（新規引用のうち同種が2社以上の場合のみ言及。1社のみでは傾向と呼べないため）
    entity_counts: dict[str, int] = {}
    for r in emerging:
        label = _ENTITY_TYPE_LABEL.get(domain_entity_types.get(r["domain"], "unknown"), "分類未登録")
        entity_counts[label] = entity_counts.get(label, 0) + 1
    entity_note = None
    if entity_counts:
        top_label, top_count = max(entity_counts.items(), key=lambda x: x[1])
        if top_count >= 2:
            entity_note = f"新規引用{len(emerging)}社のうち{top_count}社が「{top_label}」に分類されている。"

    return {
        "stable_players": stable,
        "emerging_players": emerging,
        "disappeared_players": disappeared,
        "trend_summary": lines[:5],
        "entity_note": entity_note,
        "no_current_observation": no_current_observation,
        "weekly_snapshot_note": weekly_snapshot_note,
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


# ────────────────────────────────────────────────────────────────────
# 市場存在感サマリー（レポート冒頭。ルールベース、LLM不使用）
# ────────────────────────────────────────────────────────────────────

# 順位帯ラベル（下限（超）, 上限（以下）, ラベル, ラベルに傾向語を付与するか）。
# 順位は小数（例: 10.2位）を取るため、区間は隙間なく連続させる（下限は超過、上限は以下）。
# 「1ページ目目前」等それ自体が状態を表す帯は傾向語を付けない（冗長になるため）。
# 「◯位圏」の帯（16-20・21-30・30超）のみ「で改善中／横ばい／低下中」を付けて具体度を補う。
_SEO_RANKING_BANDS = [
    (0, 3, "上位定着", False),
    (3, 10, "1ページ目", False),
    (10, 15, "1ページ目目前", False),
    (15, 20, "20位圏", True),
    (20, 30, "30位圏", True),
    (30, float("inf"), "圏外", True),
]


def _ranking_band(position: float) -> tuple[str, bool]:
    for lo, hi, label, append_trend in _SEO_RANKING_BANDS:
        if lo < position <= hi:
            return label, append_trend
    return "圏外", True


def build_market_presence_summary(
    key_kws: list[dict],
    google_aio: list[dict],
    gaio_previous: list[dict] | None,
    queries: list[str],
) -> list[dict]:
    """対象市場ごとにSEO順位・AIO引用の週次方向性（up/hold/down）を1行サマリーで返す。
    SEOの改善/低下判定は順位差だけでなく順位帯も考慮する（10位前後は僅かな差でも意味が大きいため）。
    単純な±2.0位閾値だと「12.1→10.2（-1.9）」のような1ページ目到達間際の改善を横ばい扱いしてしまう。"""
    kw_by_query = {r["query"]: r for r in key_kws}
    aio_by_query = {r["query"]: r for r in google_aio}
    prev_aio_by_query = {r["query"]: r for r in (gaio_previous or [])}

    out = []
    for q in queries:
        kw = kw_by_query.get(q)
        cur_pos = kw["cur"]["position"] if kw else 0.0
        prv_pos = kw["prv"]["position"] if kw else 0.0
        has_prv_seo = bool(kw and kw["prv"]["impressions"] > 0)

        if not has_prv_seo:
            seo_direction = "hold"
        else:
            diff = cur_pos - prv_pos  # 負=改善
            near_page1 = cur_pos <= 12
            improving = diff <= -2.0 or (near_page1 and diff <= -1.0)
            declining = diff >= 2.0 or (near_page1 and diff >= 1.0)
            seo_direction = "up" if improving else ("down" if declining else "hold")

        aio_cur = aio_by_query.get(q)
        aio_prev = prev_aio_by_query.get(q)
        tcd_cited = bool(aio_cur.get("tcd_cited")) if aio_cur else False
        tcd_cited_prev = bool(aio_prev.get("tcd_cited")) if aio_prev else None
        competitor_cited = bool(aio_cur.get("competitor_domains")) if aio_cur else False

        if tcd_cited_prev is None:
            aio_direction = "hold"
        elif tcd_cited and not tcd_cited_prev:
            aio_direction = "up"
        elif not tcd_cited and tcd_cited_prev:
            aio_direction = "down"
        else:
            aio_direction = "hold"

        if aio_direction == "down":
            overall = "down"
        elif seo_direction == "down" and aio_direction != "up":
            overall = "down"
        elif seo_direction == "up" or aio_direction == "up":
            overall = "up"
        else:
            overall = "hold"

        trend_word = {"up": "改善中", "down": "低下中", "hold": "横ばい"}[seo_direction]
        if not has_prv_seo:
            seo_status = "データ不足"
            seo_comment = "SEOは表示回数が少なく判断材料が不足"
        else:
            band_label, append_trend = _ranking_band(cur_pos)
            seo_status = f"{band_label}で{trend_word}" if append_trend else band_label
            seo_comment = f"SEOは{seo_status}"
        aio_comment = "AIO引用を獲得" if tcd_cited else "AIO引用は未獲得"
        comment = f"{seo_comment}。{aio_comment}。"

        if tcd_cited:
            aio_status = "引用あり"
        elif competitor_cited:
            aio_status = "競合のみ引用"
        else:
            aio_status = "引用なし"

        market_status = {"up": "上昇中", "hold": "横ばい", "down": "低下中"}[overall]

        # 市場優先度: 市場機会の大きさを示す軸であり、Action Itemsの実行優先度（別軸）とは一致しない。
        # 競合だけが引用され、かつ10〜20位圏でSEOが競える位置にいる市場を最優先とする
        # （検索需要があり、AIOでも接近可能な市場を取りこぼさないため）。
        contestable = has_prv_seo and 0 < cur_pos <= 20
        if overall == "down":
            priority = "高"
        elif competitor_cited and not tcd_cited and contestable:
            priority = "高"
        elif overall == "up" or (competitor_cited and not tcd_cited):
            priority = "中"
        else:
            priority = "低"

        out.append({
            "market": q,
            "seo": {"current_position": round(cur_pos, 1) if has_prv_seo or cur_pos else 0.0,
                    "previous_position": round(prv_pos, 1) if has_prv_seo else None,
                    "direction": seo_direction, "status": seo_status},
            "aio": {"tcd_cited": tcd_cited, "competitor_cited": competitor_cited,
                    "direction": aio_direction, "status": aio_status},
            "overall": overall,
            "market_status": market_status,
            "market_priority": priority,
            "comment": comment,
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


def compute_index_monitoring(
    entries: list[dict],
    cur_df: pd.DataFrame,
    prv_df: pd.DataFrame,
    site_url: str,
    service: Any = None,
    cur_end: str | None = None,
) -> list[dict]:
    """新規・更新ページがSearch Console上に出始めたかを追跡する。
    注: Search Analytics APIにはインデックス有無の情報がないため、
    「indexed」（クロール済みだが表示回数がごく僅か）はURL Inspection API連携が必要で厳密判定は未対応。
    ここでは公開日以降の日次データから初回Impression日・初回Click日を実測し、
    not_detected / indexed / gaining_impressions / gaining_clicks の4状態で判定する。"""
    base = site_url.rstrip("/")
    today = datetime.today().date()
    out = []
    for e in entries:
        path  = e.get("url", "")
        title = e.get("title", path)
        pub_date = e.get("published_or_updated_date", "")
        full_url = (base + path) if path.startswith("/") else path

        cur = _lookup_page(cur_df, full_url)

        days_since_publish = None
        if pub_date:
            try:
                days_since_publish = (today - datetime.strptime(pub_date, "%Y-%m-%d").date()).days
            except ValueError:
                days_since_publish = None

        first_impression_date, first_click_date = (None, None)
        if service is not None and pub_date and cur_end:
            first_impression_date, first_click_date = fetch_page_first_dates(
                service, site_url, full_url, pub_date, cur_end
            )

        if first_impression_date is None:
            status = "not_detected"
        elif cur["clicks"] > 0:
            status = "gaining_clicks"
        elif cur["impressions"] >= 5:
            status = "gaining_impressions"
        else:
            status = "indexed"

        out.append({
            "page_title":            title,
            "url":                   full_url,
            "published_or_updated_date": pub_date,
            "days_since_publish":    days_since_publish,
            "first_impression_date": first_impression_date,
            "first_click_date":      first_click_date,
            "current_impressions":   cur["impressions"],
            "current_clicks":        cur["clicks"],
            "current_position":      cur["position"],
            "status":                status,
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


COMPETITOR_ENTITY_TYPES = ("branding_company", "consulting_company")


def is_competitor_entity(entity_type: str) -> bool:
    """ブランディング会社・コンサル会社など、コンテンツ戦略上の「競合」とみなすentity_typeか。"""
    return entity_type in COMPETITOR_ENTITY_TYPES


def aio_competitor_ranking(gaio: list[dict], entity_types: dict | None = None) -> tuple[list[dict], int]:
    """AIOに引用された企業をドメイン単位でAIO登場率順にランキング化する（TCD含む）。

    集計キーは正規化済みドメイン。URLが取得できずドメインを解決できない参照は、
    表示名（会社名）ごとに別エンティティとして集計するが、出力のdomainフィールドは
    空欄のままにする（会社名をdomain列へ代入しない）。
    Google検索・Google自身のドメインは競合ランキングから除外する
    （AIO登場率の定義上「競合」ではないため）。

    AIO登場率 = そのドメインが引用されたAIO件数 ÷ AIO表示クエリ数。
    1つのAIOに複数ドメインが引用され得るため、合計は100%にならない。
    Returns (ranking_list, total_aio_count)
    """
    entity_types = entity_types or {}
    total_aio = sum(1 for r in gaio if r.get("aio_exists"))
    if total_aio == 0:
        return [], 0

    counts: dict[str, int] = {}
    kw_map: dict[str, list[str]] = {}
    display_names: dict[str, dict[str, int]] = {}
    domain_by_key: dict[str, str] = {}
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
        domains = r.get("cited_domains", [])
        names = r.get("cited_display_names", [])
        for i, domain in enumerate(domains):
            name = names[i] if i < len(names) else (domain or "")
            # domainが解決できていない参照（Google内部ビューアリンク等）はsource名が"Google"になるため、
            # domain単体でのGoogle判定だけでなくname側もチェックしないと素通りしてしまう。
            if (domain and ("tcd" in domain.lower() or is_google_domain(domain))) or (not domain and is_google_domain(name)):
                continue
            if not domain and not name:
                continue
            # domainを解決できない参照は表示名単位で別エンティティとして扱う
            # （domain欄は空欄のまま。会社名を代入しない）
            key = domain if domain else f"__unresolved__:{_normalize_query(name)}"
            domain_by_key[key] = domain
            counts[key] = counts.get(key, 0) + 1
            if q and q not in kw_map.get(key, []):
                kw_map.setdefault(key, []).append(q)
            display_names.setdefault(key, {})
            display_names[key][name] = display_names[key].get(name, 0) + 1

    def _best_display_name(key: str) -> str:
        names = display_names.get(key)
        if not names:
            return domain_by_key.get(key, key)
        return max(names.items(), key=lambda kv: kv[1])[0]

    ranking = sorted(
        [{
            "domain": domain_by_key.get(k, ""),
            "name": _best_display_name(k),
            "display_name": _best_display_name(k),
            "entity_type": entity_types.get(domain_by_key.get(k, ""), "unknown") if domain_by_key.get(k) else "unknown",
            "count": v,
            "appearance_rate": round(v / total_aio * 100, 1),
            "share": round(v / total_aio * 100, 1),  # 後方互換フィールド
            "keywords": kw_map.get(k, []),
        }
         for k, v in counts.items()],
        key=lambda x: -x["count"]
    )
    ranking.append({
        "domain": "tcd.jp",
        "name": "TCD（自社）",
        "display_name": "TCD（自社）",
        "entity_type": "branding_company",
        "count": tcd_count,
        "appearance_rate": round(tcd_count / total_aio * 100, 1),
        "share": round(tcd_count / total_aio * 100, 1),
        "keywords": tcd_kws,
        "is_tcd": True,
    })
    return ranking, total_aio


def split_ranking_by_entity_type(ranking: list[dict]) -> tuple[list[dict], list[dict]]:
    """AIO競合ランキングを「競合（ブランディング/コンサル会社）」と「参考情報源（それ以外）」に分割する。
    TCD自身は競合表に含める。"""
    competitors = [r for r in ranking if r.get("is_tcd") or is_competitor_entity(r.get("entity_type", "unknown"))]
    reference_sources = [r for r in ranking if not r.get("is_tcd") and not is_competitor_entity(r.get("entity_type", "unknown"))]
    return competitors, reference_sources


def gen_aio_competitor_hypothesis(ranking: list[dict], total_aio: int, period_label: str = "今週") -> str:
    """AIO競合ランキングの仮説テキストを2〜3文で生成する。
    観測事実と仮説を分け、Googleの評価意図は断定しない（「可能性」「事例が見られる」を使う）。"""
    if not ranking or total_aio == 0:
        return ""
    competitors, _ = split_ranking_by_entity_type(ranking)
    cited = [r for r in competitors if not r.get("is_tcd") and r["count"] > 0]
    tcd_entry = next((r for r in ranking if r.get("is_tcd")), None)
    parts = []
    if cited:
        top_names = "・".join(r["display_name"] for r in cited[:2])
        parts.append(f"{top_names}は比較・一覧コンテンツを保有している。")
    parts.append(
        "AIOでは、比較・一覧・定義情報を1ページ内で整理したコンテンツが引用される事例が見られる。"
    )
    if tcd_entry and tcd_entry["count"] == 0:
        parts.append(
            f"TCDは{period_label}引用0件。比較・一覧型コンテンツはAIO引用獲得に向けた有力な仮説の一つ。"
        )
    elif tcd_entry:
        parts.append(
            f"TCDは{period_label}AIO登場率{tcd_entry['appearance_rate']:.0f}%を獲得"
            f"（AIO表示{total_aio}件中{tcd_entry['count']}件で引用）。"
            f"さらなる引用拡大には比較コンテンツの強化が有効と考えられる。"
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


def _is_tcd_domain(domain: str) -> bool:
    return "tcd" in (domain or "").lower()


def build_aio_competitor_status(
    gaio_current: list[dict],
    gaio_previous: list[dict] | None,
    queries: list[str],
    domain_aliases: dict | None = None,
) -> list[dict]:
    """市場（クエリ）ごとに「今週誰が引用されたか」「前週比の新規・消失」「TCD状況」だけをまとめる。
    URL一覧・SERP安定度等の詳細はここでは扱わない（レポート本文では冗長なため）。
    compare_point_watch() のドメイン差分ロジックをそのまま再利用し、TCD自身は企業リストから除く。"""
    pw = compare_point_watch(gaio_current, gaio_previous, queries, domain_aliases=domain_aliases)
    cur_by_query = {r["query"]: r for r in gaio_current}

    out = []
    for row in pw:
        cur = cur_by_query.get(row["query"], {})
        out.append({
            "query": row["query"],
            "aio_exists": row["aio_exists"],
            "tcd_cited": bool(cur.get("tcd_cited")),
            "cited_companies": [d for d in row["cited_domains"] if d and not _is_tcd_domain(d)],
            "new_companies": [d for d in row["new_domains"] if d and not _is_tcd_domain(d)],
            "lost_companies": [d for d in row["lost_domains"] if d and not _is_tcd_domain(d)],
            "has_previous": row["has_previous"],
        })
    return out


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
    """None（前期実績0のため比較不能）は「NEW」と表示すると新規公開と誤解されるため、
    比較不能を意味する「—」に統一する（新規公開の判定はindex_monitoringで別途行う）。"""
    if c is None:
        return "—"
    return f"+{c:.1f}%" if c > 0 else f"{c:.1f}%"

def fpos_chg(c: float) -> str:
    if abs(c) < 0.05:
        return "±0.0"
    return f"+{c:.1f}" if c > 0 else f"{c:.1f}"

def fctr_chg(c: float) -> str:
    return f"+{c * 100:.2f}pp" if c >= 0 else f"{c * 100:.2f}pp"

def pos_trend_label(pos_d: float) -> str:
    """SC平均順位の変化を3段階（横ばい/小幅/明確）で分類する。
    pos_d は cur - prv（プラス=順位低下、マイナス=順位改善）。
    0.3未満=横ばい、0.3〜1.0未満=小幅、1.0以上=明確、という閾値は
    順位1桁の変動をノイズと誤解しないための基準。"""
    a = abs(pos_d)
    if a < 0.3:
        return "横ばい"
    direction = "低下" if pos_d > 0 else "改善"
    magnitude = "明確な" if a >= 1.0 else "小幅"
    return f"{magnitude}{direction}"


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
    cmp_label = "前週比" if data.get("period_type") == "weekly" else "前月比"
    good: list[str] = []
    bad: list[str] = []
    next_check: list[str] = []

    cc = pct_chg(tc["clicks"], tp["clicks"])
    if cc is not None:
        if cc > 5:
            good.append(f"総クリック数が{cmp_label} **{cc:.1f}% 増加**")
        elif cc < -5:
            bad.append(f"総クリック数が{cmp_label} **{abs(cc):.1f}% 減少**")

    pos_d = tc["position"] - tp["position"]
    if pos_d < -0.3:
        good.append(f"平均掲載順位が **{pos_trend_label(pos_d)}**（{abs(pos_d):.1f}位、{tp['position']:.1f} → {tc['position']:.1f}）")
    elif pos_d > 0.3:
        bad.append(f"平均掲載順位が **{pos_trend_label(pos_d)}**（{pos_d:.1f}位、{tp['position']:.1f} → {tc['position']:.1f}）")

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
            good.append(
                f"重点KW「**{r['query']}**」順位 {r['prv']['position']:.1f}位 → {r['cur']['position']:.1f}位"
                f"（{abs(r['pos_chg']):.1f}位改善）"
            )
        if r["pos_chg"] > 3:
            bad.append(
                f"重点KW「**{r['query']}**」順位 {r['prv']['position']:.1f}位 → {r['cur']['position']:.1f}位"
                f"（{r['pos_chg']:.1f}位低下）"
            )
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
    """Search Console と AIO を横断し、変化の背景仮説を閾値判定で生成する。
    競合の出現・消失や市場単位の存在感変化を優先的に扱い、サイト全体指標の診断はその後に続ける。"""
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    insights: list[str] = []

    # 市場・競合視点の診断（市場存在感サマリー・AIO市場変化データを横断して評価する）
    mps = data.get("market_presence_summary", [])
    if mps:
        competitor_only = [m for m in mps if m["aio"]["competitor_cited"] and not m["aio"]["tcd_cited"]]
        if competitor_only:
            names = "・".join(f'「{m["market"]}」' for m in competitor_only[:3])
            insights.append(
                f"AIO上では、対象6市場中{len(competitor_only)}市場（{names}等）で競合のみが引用され、TCD未引用の状態が続いている。"
            )
        declining = [m for m in mps if m["overall"] == "down"]
        if declining:
            names = "・".join(f'「{m["market"]}」' for m in declining[:3])
            insights.append(f"{names} で市場存在感が低下方向。優先的な確認が必要。")

    ct = data.get("citation_trends", {})
    emerging = ct.get("emerging_players", [])
    disappeared = ct.get("disappeared_players", [])
    if emerging:
        names = "・".join(r["domain"] for r in emerging[:3])
        insights.append(
            f"AIO引用元に{names}など{len(emerging)}社が新規に加わった。競合側のコンテンツ更新・SERP変動の可能性がある。"
        )
    if disappeared:
        names = "・".join(r["domain"] for r in disappeared[:3])
        insights.append(
            f"前週まで引用されていた{names}など{len(disappeared)}社の引用が消失。SERP構成が流動的な可能性がある。"
        )

    imp_chg   = pct_chg(tc["impressions"], tp["impressions"])
    click_chg = pct_chg(tc["clicks"], tp["clicks"])
    pos_d     = tc["position"] - tp["position"]
    ctr_d     = tc["ctr"] - tp["ctr"]

    # クリック変化の要因診断（検索需要 vs 順位 vs CTR）
    # 注: Search Consoleの数値だけでは「検索需要そのものの減少」と
    # 「表示対象クエリ構成の変化」「SERP要素の変化」「AIOによるゼロクリック化」を
    # 切り分けられないため、"主因は〜である" と断定せず、事実（fact）と
    # 仮説（hypothesis）を分けて記述する。
    if click_chg is not None:
        if click_chg < -5:
            if imp_chg is not None and imp_chg < -5 and abs(imp_chg) >= abs(pos_d) * 2:
                insights.append(
                    f"[事実] クリック減（{fchg(click_chg)}）にはImpression減少（{fchg(imp_chg)}）の影響が大きい。"
                    "[仮説] ただし検索需要そのものの減少、表示対象クエリ構成の変化、SERP要素の変化、"
                    "AIOによるゼロクリック化の影響は現時点では分離できない。"
                )
            elif pos_d > 0.3:
                insights.append(
                    f"[事実] クリック減（{fchg(click_chg)}）と同時に平均順位が{pos_trend_label(pos_d)}"
                    f"（{fpos_chg(pos_d)}）している。[仮説] 順位低下がクリック減に寄与している可能性がある。"
                )
            elif ctr_d < -0.005:
                insights.append(
                    f"[事実] クリック減（{fchg(click_chg)}）と同時にCTRが{ctr_d*100:.2f}pp低下している。"
                    "[仮説] SERP表示（AI Overviewなど）の変化がCTR低下に影響した可能性があり、確認したい。"
                )
            else:
                insights.append(f"[事実] クリック減（{fchg(click_chg)}）が発生しているが、単一指標には集約できず、複合的な要因の可能性がある。")
        elif click_chg > 5:
            if pos_d < -0.3:
                insights.append(f"[事実] クリック増（{fchg(click_chg)}）と同時に平均順位が{pos_trend_label(pos_d)}している。[仮説] 順位改善が寄与している可能性がある。")
            elif imp_chg is not None and imp_chg > 5:
                insights.append(f"[事実] クリック増（{fchg(click_chg)}）と同時にImpressionも{fchg(imp_chg)}増加している。[仮説] 検索需要の増加が寄与している可能性がある。")

    # AIO状況の変化診断（日次モニターデータを優先、なければ定点観測にフォールバック）
    # 観測日数が少ないうちは「安定している」と断定しない
    #（observed_days<3: 判定不可 / 3〜6: 暫定 / 7以上: 週次判定）
    aio_summary = data.get("aio_daily_summary", {})
    if aio_summary and aio_summary.get("aio_query_count", 0) > 0:
        observed_days = aio_summary.get("observed_days", 0)
        changed_count = aio_summary["aio_text_changed_count"]
        if observed_days < AIO_DAILY_MIN_REFERENCE_DAYS:
            insights.append(f"観測{observed_days}日分のため、AIO本文および表示の安定性はまだ判定できない。")
        elif changed_count == 0:
            prefix = "" if aio_summary.get("supports_weekly_comparison") else "暫定的に、"
            insights.append(f"{prefix}観測期間中、AIO本文の変化は検出されていない。")
        else:
            insights.append(
                f"観測クエリのうち{changed_count}件でAIO本文の変化を検出。SERP構成が流動的な可能性がある。"
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

    # Definition記事の伸長診断（絶対増加数を必ず併記。前週母数が小さい場合はその旨を明記）
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
        prv_imp = top["prv"]["impressions"]
        cur_imp = top["cur"]["impressions"]
        chg = pct_chg(cur_imp, prv_imp)
        small_base = " 前週母数は小さいが、" if prv_imp < 20 else " "
        insights.append(
            f"マガジン系「{top['name']}」は{fi(prv_imp)} → {fi(cur_imp)} Impressionへ増加"
            f"（+{fi(cur_imp - prv_imp)}、{fchg(chg)}）。{small_base}露出が拡大している。"
        )

    if not insights:
        insights.append("今期は指標間で明確な因果関係は検出されず、大きな変化のない安定期と考えられる。")

    return insights[:6]


def gen_ai_insights(data: dict) -> list[str]:
    """AI Insights セクション本文。現状はルールベース実装のみを使用する。"""
    return gen_ai_insights_rule_based(data)


def _review_date(weeks: int) -> str:
    return (datetime.today() + timedelta(weeks=weeks)).strftime("%Y-%m-%d")


_SIGNAL_LABEL_JA = {
    "impressions_increase": "Impression増加",
    "position_improvement": "順位改善",
    "tcd_aio_citation": "TCDのAIO引用",
    "ctr_recovery": "CTR回復",
    "top20_entry": "上位20位以内到達",
}


def _signal_label(code: str) -> str:
    return _SIGNAL_LABEL_JA.get(code, code)


def _action_item(priority: int, title: str, evidence: str, target: str, content_type: str, action: str,
                  expected_signal: list[str], review_weeks: int = 4) -> dict:
    """priority/title/evidence/target/content_type/action/expected_signal/review_date を構造化して返す。
    Markdown版は入れ子箇条書き、HTML版はカードレイアウトへそれぞれ整形する
    （1行に詰め込むと可読性が下がるため、文字列ではなくdictで保持する）。
    expected_signalは内部コード（例: tcd_aio_citation）を日本語ラベルへ変換して表示する。"""
    return {
        "priority": priority,
        "title": title,
        "evidence": evidence,
        "target": target,
        "content_type": content_type,
        "action": action,
        "expected_signal": [_signal_label(s) for s in expected_signal],
        "review_date": _review_date(review_weeks),
        "review_weeks": review_weeks,
    }


def _action_items_md(actions: list[dict]) -> str:
    """Action Itemsを入れ子箇条書きで整形する（1行への詰め込みを避ける）。"""
    lines = []
    for a in actions:
        lines.append(f'- **[優先度{a["priority"]}] {a["title"]}**')
        lines.append(f'  - 根拠: {a["evidence"]}')
        lines.append(f'  - 対象: {a["target"]}（{a["content_type"]}）')
        lines.append(f'  - 施策: {a["action"]}')
        signal_txt = "・".join(a["expected_signal"]) if a["expected_signal"] else "-"
        lines.append(f'  - 期待シグナル: {signal_txt}')
        lines.append(f'  - 確認日: {a["review_date"]}（{a["review_weeks"]}週間後）')
    return "\n".join(lines) + "\n"


# suggested_action（compute_aio_opportunityの短いラベル）を、実務担当者が着手できる具体的な
# ページ企画指示へ展開するマッピング。「検討する」で終わらせず、含めるべき要素まで明示する。
_AIO_OPPORTUNITY_CONCRETE_ACTION = {
    "比較表追加": "比較観点・会社タイプ・相談前チェックリスト・FAQ・関連Definitionリンクを含む比較ページ企画を作成する。",
    "title・description見直し": "title・meta descriptionを検索意図（比較・選び方）に即した文言へ書き換える。",
    "FAQ追加": "想定質問と回答を明示したFAQセクションをページ内に追加する。",
    "SERP動向を継続監視（AIOが未表示のため引用機会自体が未発生）": "AIO表示の有無を継続監視し、AIOが表示され始めたタイミングで比較コンテンツを追加投入する。",
}


def gen_action_items_rule_based(data: dict) -> list[dict]:
    """来週やるべき実務タスクを、タイトル・根拠・対象・具体的施策・期待シグナル・確認日つきで抽出する。
    戻り値は構造化dictのリスト（レンダリングはMarkdown/HTMLそれぞれの専用関数で行う）。"""
    actions: list[dict] = []
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    priority = 1

    # 新規/更新ページの追跡（実施済み施策・公開済みページを認識してAction化）
    idx_mon = data.get("index_monitoring", [])
    gaining = [p for p in idx_mon if p["status"] in ("gaining_impressions", "gaining_clicks")]
    for p in gaining[:2]:
        evidence = (
            f'「{p["page_title"]}」は公開から{p.get("days_since_publish", "-")}日で '
            f'Impression {fi(p["current_impressions"])}件・Click {fi(p["current_clicks"])}件を記録。'
        )
        actions.append(_action_item(
            priority, f'「{p["page_title"]}」の追跡強化',
            evidence, p["url"], "公開済みページ",
            "インデックス状況・順位・AIO引用有無を週次で記録し、関連Definition記事から内部リンクを追加する。",
            ["impressions_increase", "position_improvement", "tcd_aio_citation"],
        ))
        priority += 1

    ctr_d = tc["ctr"] - tp["ctr"]
    if ctr_d < -0.005:
        actions.append(_action_item(
            priority, "CTR低下要因の切り分け",
            f"CTRが{ctr_d*100:.2f}pp低下（{fp(tp['ctr'])} → {fp(tc['ctr'])}）。",
            "サイト全体", "SERP表示要素",
            "Impression上位クエリのSERPを目視確認し、AI Overview・強調スニペットの表示有無で影響を切り分ける。",
            ["ctr_recovery"], review_weeks=2,
        ))
        priority += 1

    opp = data.get("aio_opportunity", [])
    if opp:
        top = opp[0]
        concrete = _AIO_OPPORTUNITY_CONCRETE_ACTION.get(
            top.get("suggested_action", ""), "Definition記事を新規作成し、比較・選び方の観点を追加する。"
        )
        actions.append(_action_item(
            priority, f'「{top["keyword"]}」のAIO引用獲得',
            f'「{top["keyword"]}」はAIO未引用（Imp {fi(top.get("impressions", 0))}件・順位{fpos(top.get("position", 0))}位）。',
            top["keyword"], "比較・選び方ページ",
            concrete,
            ["tcd_aio_citation", "impressions_increase"],
        ))
        priority += 1

    # 競合対策: ブランディング/コンサル会社に限定し、銀行・行政・メディア等は対象にしない
    ranking, _ = aio_competitor_ranking(data.get("google_aio", []), entity_types=data.get("domain_entity_types", {}))
    competitors, _ref_sources = split_ranking_by_entity_type(ranking)
    top_competitor = next((r for r in competitors if not r.get("is_tcd") and r["count"] > 0), None)
    if top_competitor:
        cited_queries = top_competitor.get("keywords", [])
        target_query = cited_queries[0] if cited_queries else "（対象クエリ不明）"
        domain_txt = top_competitor["domain"] or "ドメイン未解決"
        # count（引用回数の合計）は複数市場にまたがる合算値であり、target_query1件分の引用数ではない。
        # 「{target_query}のAIOでN件引用」のように単一クエリの数値であるかのような表現にしないよう分けて記述する。
        queries_txt = "・".join(f'「{k}」' for k in cited_queries[:6])
        actions.append(_action_item(
            priority, f'競合「{top_competitor["display_name"]}」への対抗コンテンツ',
            f'「{top_competitor["display_name"]}」（{domain_txt}）は観測6市場中{len(cited_queries)}市場'
            f'（{queries_txt}）でAIOに引用されている（引用回数計{top_competitor["count"]}件、'
            f'AIO登場率{top_competitor["appearance_rate"]:.0f}%）。',
            target_query, "比較・選び方ページ",
            "比較観点・会社タイプ・相談前チェックリスト・FAQ・関連Definitionリンクを含むページ企画を作成する。",
            ["tcd_aio_citation"],
        ))
        priority += 1

    dropping = sorted(
        [r for r in data.get("key_kws", []) if r["prv"]["impressions"] > 0 and r["pos_chg"] > 3],
        key=lambda r: -r["pos_chg"],
    )
    if dropping:
        top = dropping[0]
        actions.append(_action_item(
            priority, f'重点KW「{top["query"]}」の順位低下要因確認',
            f'重点KW「{top["query"]}」の順位が{top["prv"]["position"]:.1f}位 → {top["cur"]["position"]:.1f}位'
            f'（{top["pos_chg"]:.1f}位低下）。',
            top["query"], "重点キーワードページ",
            "上位10件の見出し構成・コンテンツ形式（比較表・FAQ有無等）を比較し、SERP構成の変化点を洗い出す。",
            ["position_improvement"], review_weeks=2,
        ))
        priority += 1

    growing_def = [
        r for r in data.get("def_pages", [])
        if r["prv"]["impressions"] > 0 and r["click_chg"] is not None and r["click_chg"] > 20
    ]
    if growing_def:
        top = growing_def[0]
        actions.append(_action_item(
            priority, f'「{top["name"]}」の伸長を横展開',
            f'マガジン系「{top["name"]}」のクリックが{top["click_chg"]:.0f}%伸長。',
            top["url"], "関連マガジン記事",
            f'「{top["name"]}」と関連性の高いテーマで新規記事を1本企画し、相互内部リンクを設置する。',
            ["impressions_increase"],
        ))
        priority += 1

    # 重複削減: 同一対象（target）に対して複数の施策が生成された場合、
    # 最初に生成された（＝優先度の高い）ものだけを残し、優先度を振り直す。
    seen_targets: set[str] = set()
    deduped: list[dict] = []
    for a in actions:
        key = _normalize_query(a["target"])
        if key in seen_targets:
            continue
        seen_targets.add(key)
        deduped.append(a)
    for i, a in enumerate(deduped, start=1):
        a["priority"] = i
    actions = deduped

    if not actions:
        actions.append(_action_item(
            1, "現状維持", "大きな変化は検出されなかった。",
            "サイト全体", "モニタリング",
            "既存監視を継続し、特別なアクションは行わない。",
            [], review_weeks=1,
        ))

    return actions[:5]


def gen_action_items(data: dict) -> list[dict]:
    """Action Items セクション本文。現状はルールベース実装のみを使用する。"""
    return gen_action_items_rule_based(data)


def gen_insights(data: dict) -> str:
    good, bad, next_check = _calc_insights(data)
    cmp_label = "前週比" if data.get("period_type") == "weekly" else "前月比"
    next_label = "次週確認ポイント・改善推奨" if data.get("period_type") == "weekly" else "次月確認ポイント・改善推奨"
    lines: list[str] = ["### 伸びた項目"]
    lines += [f"- ✅ {g}" for g in good] if good else [f"- {cmp_label}で明確な改善項目は検出されませんでした。"]
    lines += ["", "### 落ちた項目"]
    lines += [f"- ⚠️ {b}" for b in bad] if bad else [f"- {cmp_label}で明確な悪化項目は検出されませんでした。"]
    lines += ["", f"### {next_label}"]
    if not next_check:
        next_check.append("引き続きデータ蓄積を継続。特段の急変がなければ現状維持で問題なし。")
    lines += [f"- {n}" for n in next_check]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# Executive Summary（経営層向け要約・3〜5項目）
# ────────────────────────────────────────────────────────────────────

def gen_executive_summary(data: dict) -> list[str]:
    """経営層が最初の30秒で状況を把握できるよう、市場存在感・検索需要・順位・CTR・AIO状況・結論の
    6項目に絞った要約を返す。結論のみ gen_ai_insights() の仮説を採用する（他は事実ベース）。
    Search Console単体の数値報告ではなく、市場での存在感の変化を先頭に置く。"""
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    period_label = "今週" if data.get("period_type") == "weekly" else "今月"
    items: list[str] = []

    # 0. 市場存在感（事実。6市場の方向性サマリー。存在しない場合はスキップ）
    mps = data.get("market_presence_summary", [])
    if mps:
        up_n = sum(1 for m in mps if m["overall"] == "up")
        down_n = sum(1 for m in mps if m["overall"] == "down")
        hold_n = len(mps) - up_n - down_n
        high_priority = [m for m in mps if m["market_priority"] == "高"]
        if high_priority:
            top = high_priority[0]
            focus_txt = f"市場機会が最も大きいのは「{top['market']}」（{top['comment']}）"
        else:
            top = mps[0]
            focus_txt = f"注目市場は「{top['market']}」（{top['comment']}）"
        items.append(
            f"**市場存在感**: 対象6市場中、上昇{up_n}件・横ばい{hold_n}件・低下{down_n}件。{focus_txt}。"
        )

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
        trend = pos_trend_label(pos_d)
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

    # 4. AIO状況（事実。週次AIOスナップショットと日次AIO推移は別データのため、両方を明示して併記する）
    gaio = data.get("google_aio", [])
    valid_gaio = [r for r in gaio if r.get("status") != "API_KEY_MISSING"]
    if valid_gaio:
        aio_shown_n = sum(1 for r in valid_gaio if r.get("aio_exists"))
        tcd_cited_n = sum(1 for r in valid_gaio if r.get("tcd_cited"))
        snapshot_txt = (
            f"週次AIOスナップショットでは観測{len(valid_gaio)}クエリ中、"
            f"AIO表示{aio_shown_n}件・TCD引用{tcd_cited_n}件。"
        )
    else:
        snapshot_txt = "週次AIOスナップショットは未取得（SERPAPI_API_KEY未設定）。"

    aio_summary = data.get("aio_daily_summary", {})
    if aio_summary and aio_summary.get("aio_query_count", 0) > 0:
        observed_days = aio_summary.get("observed_days", 0)
        if observed_days == 0:
            daily_txt = "日次AIO推移は今週分のデータが未取得のため、引用元の前週比較は判断保留。"
        elif aio_summary.get("is_reference_only"):
            daily_txt = f"日次AIO推移は観測{observed_days}日分のため参考値。"
        else:
            daily_txt = f"日次AIO推移は観測{observed_days}日分（本文変更{aio_summary['aio_text_changed_count']}件）。"
    else:
        daily_txt = "日次AIO推移は未取得（aio_daily_monitor.py未実行）。"

    items.append(f"**AIO状況**: {snapshot_txt}{daily_txt}")

    # 5. 結論（事実ベースの短い要約のみ。仮説はAI Insights側に譲り、文言を重複させない）
    items.append(f"**{period_label}の結論**: {_gen_conclusion(data)}")

    return items[:6]


def _gen_conclusion(data: dict) -> str:
    """Executive Summaryの結論行。市場存在感サマリーがあれば市場視点（どの市場が改善し、
    AIO引用にどれだけ課題があるか）で述べる。データがない場合はサイト全体指標にフォールバックする。"""
    mps = data.get("market_presence_summary", [])
    if not mps:
        return _gen_conclusion_site_wide(data)

    parts: list[str] = []

    improving = [m for m in mps if m["overall"] == "up"]
    if improving:
        names = "・".join(m["market"] for m in improving[:3])
        parts.append(f"{names}でSEO順位が改善。")

    declining = [m for m in mps if m["overall"] == "down"]
    if declining:
        names = "・".join(m["market"] for m in declining[:3])
        parts.append(f"{names}で存在感が低下方向。")

    if not improving and not declining:
        parts.append("6市場とも大きな変化はなく横ばい。")

    cited_n = sum(1 for m in mps if m["aio"]["tcd_cited"])
    if cited_n == 0:
        parts.append(f"一方、{len(mps)}市場すべてでTCDのAIO引用は未獲得。")
    else:
        parts.append(f"AIO引用は{len(mps)}市場中{cited_n}市場で獲得。")

    high_priority = [m["market"] for m in mps if m["market_priority"] == "高"]
    if high_priority:
        names = "・".join(high_priority[:2])
        parts.append(f"今週は{names}への対応を優先する。")

    conclusion = "".join(parts)
    if len(conclusion) > 150:
        conclusion = conclusion[:149] + "…"
    return conclusion


def _gen_conclusion_site_wide(data: dict) -> str:
    """市場存在感サマリー未設定時のフォールバック（旧・サイト全体指標ベースの結論）。"""
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    parts: list[str] = []

    imp_chg = pct_chg(tc["impressions"], tp["impressions"])
    click_chg = pct_chg(tc["clicks"], tp["clicks"])
    if imp_chg is not None and click_chg is not None:
        same_dir_down = imp_chg < -5 and click_chg < -5
        same_dir_up   = imp_chg > 5 and click_chg > 5
        if same_dir_down:
            parts.append(f"Impression減少（{fchg(imp_chg)}）に伴いクリック数も減少。")
        elif same_dir_up:
            parts.append(f"Impression増加（{fchg(imp_chg)}）に伴いクリック数も増加。")
        else:
            parts.append(f"Impressionは{fchg(imp_chg)}、クリックは{fchg(click_chg)}。")

    pos_d = tc["position"] - tp["position"]
    if tp["position"] > 0:
        parts.append(f"平均順位は{pos_trend_label(pos_d)}（{abs(pos_d):.1f}位）。")

    aio_summary = data.get("aio_daily_summary", {})
    if aio_summary.get("is_reference_only"):
        obs = aio_summary.get("observed_days", 0)
        parts.append(f"AIO日次観測は{obs}日分のため参考値とし、")

    idx_mon = data.get("index_monitoring", [])
    gaining_names = [p["page_title"] for p in idx_mon if p["status"] in ("gaining_impressions", "gaining_clicks")]
    growing_def_names = [
        r["name"] for r in data.get("def_pages", [])
        if r["prv"]["impressions"] > 0
        and pct_chg(r["cur"]["impressions"], r["prv"]["impressions"]) is not None
        and pct_chg(r["cur"]["impressions"], r["prv"]["impressions"]) > 20
    ]
    watch = growing_def_names[:1] + gaining_names[:1]
    if watch:
        parts.append(f"{'と'.join(watch)}の推移を継続確認する。")
    else:
        parts.append("大きな変化のない項目は現状のモニタリングを継続する。")

    conclusion = "".join(parts)
    if len(conclusion) > 130:
        conclusion = conclusion[:129] + "…"
    return conclusion


# ────────────────────────────────────────────────────────────────────
# Markdown レポート生成
# ────────────────────────────────────────────────────────────────────

_ENTITY_TYPE_LABEL_JA = {
    "branding_company": "ブランディング会社", "consulting_company": "コンサル会社",
    "client_company": "クライアント企業", "media": "メディア",
    "public_institution": "公的機関", "financial_institution": "金融機関",
    "platform": "プラットフォーム", "unknown": "不明",
}


def _ranking_table_md(items: list[dict]) -> str:
    """AIO競合ランキング／参考情報源の共通テーブル。domain列には正規化済みホスト名のみを入れ、
    解決できない場合は「-」を表示する（会社名を代入しない）。"""
    if not items:
        return ""
    lines = [
        "| 企業（表示名） | ドメイン | 種別 | 対象キーワード | AIO登場率 |",
        "|---------------|---------|------|-------------|----------|",
    ]
    for item in items:
        kw_txt = "、".join(item.get("keywords", [])) or "—"
        share_txt = f'{item["count"]}件 ({item["appearance_rate"]:.0f}%)'
        domain_txt = item.get("domain") or "-"
        entity_label = _ENTITY_TYPE_LABEL_JA.get(item.get("entity_type", "unknown"), "不明")
        lines.append(f"| {item['display_name']} | {domain_txt} | {entity_label} | {kw_txt} | {share_txt} |")
    return "\n".join(lines) + "\n"


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
    observed_days = summary.get("observed_days", 0)
    expected_days = summary.get("expected_days", 0)
    coverage_rate = summary.get("coverage_rate", 0.0)
    lines = [
        f"- 観測日数: {observed_days} / {expected_days}日（カバレッジ {coverage_rate*100:.0f}%）",
        f"- 観測クエリ数: {summary['aio_query_count']}",
        f"- AIO表示率: {rate*100:.0f}%（観測{observed_days}日分）" if rate is not None else "- AIO表示率: -",
        f"- TCD引用率: {tcd_rate*100:.0f}%（観測{observed_days}日分）" if tcd_rate is not None else "- TCD引用率: -",
        f"- 前回観測からの本文変更件数: {summary['aio_text_changed_count']}",
    ]
    if summary.get("error_count"):
        lines.append(f"- エラー件数: {summary['error_count']}")
    if summary.get("is_reference_only"):
        lines.append(
            f"- ⚠️ 日次観測開始直後のため（観測{observed_days}日 < {AIO_DAILY_MIN_REFERENCE_DAYS}日）、"
            "AIO表示率・引用率は参考値です。"
        )
    if not summary.get("supports_weekly_comparison"):
        lines.append(
            f"- 観測日数が{AIO_DAILY_MIN_COMPARISON_DAYS}日未満のため、AIO日次データの週次比較は行っていません。"
        )
    return "\n".join(lines) + "\n"


def _aio_query_daily_table_md(query_level: list[dict]) -> str:
    """観測日数が少なくても、表示率・引用率へ毎行「（参考値）」は付けない
    （表の直前に一度だけ注意書きを出す運用に統一。観測日数列は残す）。"""
    if not query_level:
        return "AIO日次データ未取得。\n"
    lines = [
        "| クエリ | 観測日数 | AIO表示日数 | AIO表示率 | TCD引用日数 | TCD言及日数 | 最新ステータス | 本文変更回数 |",
        "|-------|--------|-----------|----------|-----------|-----------|--------------|------------|",
    ]
    for r in query_level:
        rate   = f"{r['aio_exists_rate']*100:.0f}%" if r["aio_exists_rate"] is not None else "-"
        status = _AIO_DAILY_STATUS_LABEL_MD.get(r["latest_status"], r["latest_status"])
        lines.append(
            f"| {r['query']} | {r.get('observed_days', '-')} | {r['aio_exists_days']} | {rate} "
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


def _citation_trends_player_table_md(rows: list[dict], label: str) -> list[str]:
    if not rows:
        return [f"**{label}**: 該当なし", ""]
    out = [f"**{label}**", "", "| ドメイン | 引用回数 | 対象クエリ |", "|---|---|---|"]
    for r in rows:
        queries = "・".join(r["related_queries"][:3]) or "-"
        out.append(f"| {r['domain']} | {r['cited_count']} | {queries} |")
    out.append("")
    return out


def _citation_trends_md(ct: dict) -> str:
    lines = ["**今週の変化サマリー**", ""]
    for s in ct.get("trend_summary", []):
        lines.append(f"- {s}")
    if ct.get("entity_note"):
        lines.append(f"- {ct['entity_note']}")
    if ct.get("weekly_snapshot_note"):
        lines.append(f"- {ct['weekly_snapshot_note']}")
    lines.append("")
    # 日次AIO推移データが未取得の週は、継続/新規/消失の3表を空欄で並べても情報がないため省略する
    if not ct.get("no_current_observation"):
        lines += _citation_trends_player_table_md(ct.get("stable_players", []), "継続引用（安定した存在感）")
        lines += _citation_trends_player_table_md(ct.get("emerging_players", []), "新規引用（存在感が高まった可能性）")
        lines += _citation_trends_player_table_md(ct.get("disappeared_players", []), "引用消失（前週から存在感が低下）")
    return "\n".join(lines)


def _aio_text_change_summary_md(changes: dict) -> str:
    if not changes or changes.get("changed_count", 0) == 0:
        return "対象期間中、AIO本文の変化は検出されませんでした。\n"
    lines = [f"**本文変更件数**: {changes['changed_count']}件", "", "**直近の変更クエリ:**"]
    for c in changes["changed_queries"]:
        lines.append(f"- {c['date']}: 「{c['query']}」")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────────────
# AIO競合状況（Google AI Overview観測 + 定点SERP観測の統合版）
# 「今週誰が引用されたか」「前週との差分」「TCD状況」のみを扱う。
# URL一覧・AIO本文抜粋・SERP安定度等の詳細はレポート本文では扱わない。
# ────────────────────────────────────────────────────────────────────

def _aio_competitor_status_md(rows: list[dict]) -> str:
    if not rows:
        return "観測データなし（SERPAPI_API_KEY未設定、またはgoogle_aio.enabled=falseのため）。\n"
    lines: list[str] = []
    for r in rows:
        lines.append(f"### {r['query']}")
        lines.append("")
        if not r["aio_exists"]:
            lines.append("- AIOは表示されていません。")
            lines.append("")
            continue
        cited = "・".join(r["cited_companies"]) or "なし"
        lines.append(f"- 今週引用: {cited}")
        if not r["has_previous"]:
            lines.append("- 前週比: 初回観測")
        else:
            new_txt = "・".join(r["new_companies"]) or "なし"
            lost_txt = "・".join(r["lost_companies"]) or "なし"
            lines.append(f"- 前週比: 新規 {new_txt} ／ 消失 {lost_txt}")
        lines.append(f"- TCD: {'引用あり' if r['tcd_cited'] else '未引用'}")
        lines.append("")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# ページ単体クエリ分析（地域ページ判定・AIOハブ候補判定）
# ────────────────────────────────────────────────────────────────────

_PQ_REGIONAL_STATUS_LABEL = {
    "confirmed": "地域ページとして認識済み",
    "strengthening": "地域ページとして認識が強化中",
    "weak": "地域認識は弱い（データ不足）",
    "none": "地域クエリ未検出",
}

_PQ_AIO_HUB_STATUS_LABEL = {
    "none": "一般語での表示なし",
    "initial_signal": "AIOハブ化の初期兆候あり",
    "growing": "AIOハブ候補として成長中",
    "established": "AIOハブとして定着",
}


def _page_query_recommendations(analysis: dict) -> list[str]:
    """評価結果に基づくルールベースの次アクション提案。"""
    reg_status = analysis["regional"]["status"]
    hub_status = analysis["aio_hub"]["status"]
    label = analysis["label"]
    recs: list[str] = []

    if reg_status in ("weak", "none"):
        recs.append(f"本文修正: 「{label}」内で大阪・関西の地名や地域特化ワードの言及を増やす余地があります。")
    else:
        recs.append("本文修正: 地域ページとしての認識は進んでいます。現状の構成を維持しつつ、地域クエリの継続監視を推奨します。")

    if hub_status in ("none", "initial_signal"):
        recs.append("内部リンク強化: 「ブランディング会社」「ブランディング会社 選び方」等の一般語で表示されているページ・記事から本ページへの内部リンクを増やすことを検討してください。")
    else:
        recs.append("内部リンク強化: 一般語クエリでの表示が育っています。関連するDefinition/Service記事との相互リンクをさらに強化してください。")

    recs.append("関連ページとの接続: 大阪拠点・関西エリアの実績があるService/Worksページから本ページへの導線があるか確認してください。")

    if reg_status in ("confirmed", "strengthening"):
        recs.append("東京ページへの展開: 本ページで有効だった地域キーワード構成・見出し構造を、東京ページの改善にも適用することを検討してください。")

    return recs


def _page_query_watch_md(pqw: list[dict]) -> str:
    if not pqw:
        return "監視対象ページが未設定です（config/aio_monitor.yml の page_query_watch を設定してください）。\n"

    md: list[str] = []
    for analysis in pqw:
        label = analysis["label"]
        tc = analysis["totals_current"]
        tp = analysis["totals_previous"]
        reg = analysis["regional"]
        hub = analysis["aio_hub"]
        ref_s, ref_e = analysis["reference_period"]

        md.append(f"### {label}単体の検索クエリ")
        md.append("")
        md.append(f"> 対象URL: {analysis['url']}")
        md.append(f"> 参考期間（90日目安）: {ref_s} 〜 {ref_e} / クエリ数 {len(analysis['ref_rows'])}件")
        md.append("")

        # 総況
        md.append("**総況**")
        md.append("")
        md.append("| 指標 | 今週 | 前週 |")
        md.append("|------|------|------|")
        md.append(f"| クエリ数 | {tc['query_count']} | {tp['query_count']} |")
        md.append(f"| クリック合計 | {fi(tc['clicks'])} | {fi(tp['clicks'])} |")
        md.append(f"| 表示回数合計 | {fi(tc['impressions'])} | {fi(tp['impressions'])} |")
        md.append(f"| 平均CTR | {fp(tc['ctr'])} | {fp(tp['ctr'])} |")
        md.append(f"| 平均順位 | {fpos(tc['position'])} | {fpos(tp['position'])} |")
        md.append("")

        # 地域ページとしての認識
        md.append("**地域ページとしての認識**")
        md.append("")
        md.append(
            f"- 大阪・関西を含むクエリ数: {reg['query_count']}件（表示回数合計 {fi(reg['impressions'])}、前週 {fi(reg['prv_impressions'])}）"
        )
        md.append(f"- 参考90日での地域クエリ数: {reg['ref_query_count']}件")
        if reg["new_queries"]:
            md.append(f"- 前週からの新規出現クエリ: {'・'.join(reg['new_queries'])}")
        if reg["moved_queries"]:
            moved = "・".join(f"{r['query']}({fpos_chg(r['position_diff'])})" for r in reg["moved_queries"])
            md.append(f"- 順位が大きく動いたクエリ（±3位以上）: {moved}")
        md.append(f"- 判定: **{_PQ_REGIONAL_STATUS_LABEL.get(reg['status'], reg['status'])}**")
        md.append("")

        # AIOハブ候補としての広がり
        md.append("**AIOハブ候補としての広がり**")
        md.append("")
        md.append(
            f"- 一般語（地域名を含まない）: {len(hub['general_rows'])}件 / "
            f"比較・選定系: {len(hub['comparison_rows'])}件 / 自然文: {len(hub['natural_language_rows'])}件"
        )
        if hub["new_queries"]:
            md.append(f"- 前週からの新規出現クエリ: {'・'.join(hub['new_queries'])}")
        md.append(f"- 判定: **{_PQ_AIO_HUB_STATUS_LABEL.get(hub['status'], hub['status'])}**")
        md.append(
            "> 注: 自然文・比較クエリの出現はAI Overviewへの掲載を意味しません。"
            "会話型検索に近い検索意図が生まれている兆候として参考情報にとどめてください。"
        )
        md.append("")

        # 次の対応
        md.append("**次の対応**")
        md.append("")
        for rec in _page_query_recommendations(analysis):
            md.append(f"- {rec}")
        md.append("")

    return "\n".join(md)


_MPS_DIRECTION_ARROW = {"up": "↑", "hold": "→", "down": "↓"}


def _mps_seo_cell_text(seo: dict) -> str:
    """状況セルの文言。表示回数不足でpositionが実質未計測の場合は「0.0位」等の見せかけの数値を出さない。"""
    if seo["status"] == "データ不足":
        return f"{seo['status']}（表示回数僅少）"
    prv_txt = f"{fpos(seo['previous_position'])}位" if seo["previous_position"] is not None else "データなし"
    return f"{seo['status']}（{fpos(seo['current_position'])}位 ← {prv_txt}）"


def _market_presence_summary_md(mps: list[dict]) -> str:
    lines = [
        "> 市場優先度＝市場機会の大きさ（Action Itemsの実行優先度とは別軸）。",
        "",
        "| 市場 | SEO状況 | AIO状況 | 総合 | 市場優先度 | コメント |",
        "|---|---|---|---|---|---|",
    ]
    for m in mps:
        seo, aio = m["seo"], m["aio"]
        seo_cell = _mps_seo_cell_text(seo)
        overall_cell = f"{_MPS_DIRECTION_ARROW.get(m['overall'], '?')} {m['market_status']}"
        lines.append(
            f"| {m['market']} | {seo_cell} | {aio['status']} "
            f"| {overall_cell} | {m['market_priority']} | {m['comment']} |"
        )
    return "\n".join(lines)


def gen_markdown(data: dict, config: dict) -> str:
    (cs, ce), (ps, pe) = data["periods"]
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    site_url = config["site_url"]
    top_n = config.get("report", {}).get("top_n", 20)

    period_type = data.get("period_type", "monthly")
    cur_days = (datetime.strptime(ce, "%Y-%m-%d") - datetime.strptime(cs, "%Y-%m-%d")).days + 1
    prv_days = (datetime.strptime(pe, "%Y-%m-%d") - datetime.strptime(ps, "%Y-%m-%d")).days + 1

    if period_type == "weekly":
        report_title_line = f"# TCD.jp SEO / AIO 週次レポート — {cs} 〜 {ce}"
        cur_period_label = "対象週"
        prv_period_label = "前週"
        cmp_label = "前週比"
    else:
        report_month = datetime.strptime(cs, "%Y-%m-%d").strftime("%Y年%m月")
        report_title_line = f"# TCD.jp SEO/AIO 月次レポート — {report_month}"
        cur_period_label = "前月"
        prv_period_label = "前々月"
        cmp_label = "前月比"

    md: list[str] = []

    # ── ヘッダー ──
    md += [
        report_title_line,
        "",
        f"| | 期間 |",
        f"|---|---|",
        f"| **{cur_period_label}** | {cs} 〜 {ce}（{cur_days}日間） |",
        f"| **{prv_period_label}** | {ps} 〜 {pe}（{prv_days}日間） |",
        f"| **生成日時** | {datetime.now().strftime('%Y年%m月%d日 %H:%M')} |",
        "",
        "---",
        "",
    ]

    # ── 市場存在感サマリー ──
    mps = data.get("market_presence_summary", [])
    if mps:
        md += ["## 市場存在感サマリー", ""]
        md.append(_market_presence_summary_md(mps))
        md += ["", "---", ""]

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
        "> 来週やるべき実務タスク（実行優先度順。市場存在感サマリーの「市場優先度」＝機会の大きさとは別軸）。",
        "",
    ]
    md.append(_action_items_md(gen_action_items(data)))
    md += ["", "---", ""]

    # ── 1. サイト全体サマリー ──
    md += ["## 1. サイト全体サマリー", ""]
    md += [
        f"| 指標 | {cur_period_label} | {prv_period_label} | 増減 | 増減率 |",
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
    label = pos_trend_label(pos_d)
    arrow = "↑" if pos_d < -0.3 else ("↓" if pos_d > 0.3 else "→")
    pos_trend = f"{arrow}{label}"
    md.append(
        f"| 平均掲載順位 | {fpos(tc['position'])} | {fpos(tp['position'])} "
        f"| {fpos_chg(pos_d)} | {pos_trend} |"
    )
    md.append("")

    # ── 2. 重点キーワード ──
    md += ["## 2. 重点キーワード分析", ""]
    md.append(_kw_table_md(data["key_kws"]))

    # ── 3. AIO競合状況（旧: Google AI Overview観測 + 定点SERP観測を統合） ──
    md += [
        "## 3. AIO競合状況（週次AIOスナップショット）",
        "",
        "> 今週誰が引用されたか・前週との差分（新規/消失）・TCD引用状況をまとめます。"
        "本文全体・URL一覧はreports/google_aio/配下のJSONを参照してください。",
        "",
    ]
    md.append(_aio_competitor_status_md(data.get("aio_competitor_status", [])))

    # ── 4. AIO競合ランキング / AIO参考情報源 ──
    md += ["## 4. AIO競合ランキング", ""]
    period_label = "今週" if data.get("period_type") == "weekly" else "今月"
    ranking, total_aio = aio_competitor_ranking(data.get("google_aio", []), entity_types=data.get("domain_entity_types", {}))
    competitors, reference_sources = split_ranking_by_entity_type(ranking)
    md.append(f"### {period_label}観測AIO: {total_aio}件")
    md.append("")
    md.append(_ranking_table_md(competitors[:10]))
    if not competitors:
        md.append("競合（ブランディング/コンサル会社）は検出されませんでした。\n")
    else:
        md.append("")
        md.append(
            "> AIO登場率 = そのドメインが引用されたAIO件数 ÷ AIO表示クエリ数。"
            "1件のAIOに複数ドメインが引用され得るため、合計は100%になりません。"
        )
        md.append("")
        hypothesis = gen_aio_competitor_hypothesis(ranking, total_aio, period_label)
        if hypothesis:
            md.append(f"> {hypothesis}")
            md.append("")

    md += ["### AIO参考情報源（銀行・行政・メディア等、ブランディング/コンサル会社以外の引用元）", ""]
    if reference_sources:
        md.append(_ranking_table_md(reference_sources[:10]))
        md.append("")
        md.append(f"> 全{len(reference_sources)}件のうち上位10件を表示。")
        md.append("")
    else:
        md.append("参考情報源は検出されませんでした。\n")

    # ── 6. AIO Opportunity ──
    md += [
        "## 5. AIO Opportunity",
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
        "## 6. AIO Daily Summary（日次AIO推移）",
        "",
        "> aio_daily_monitor.py（毎朝6時実行）が観測した日次AIOデータを、本レポートと同じ対象期間で集計しています。",
        "",
    ]
    md.append(_aio_daily_summary_md(data.get("aio_daily_summary", {})))

    # ── 8. クエリ別AIO日次モニター ──
    md += ["## 7. クエリ別AIO日次モニター", ""]
    _aio_summary_for_note = data.get("aio_daily_summary", {})
    if _aio_summary_for_note.get("is_reference_only"):
        obs = _aio_summary_for_note.get("observed_days", 0)
        md += [f"※ 本表は観測{obs}日分の基準データです。表示率・引用率は参考値として扱います。", ""]
    md.append(_aio_query_daily_table_md(_aio_summary_for_note.get("query_level", [])))

    # ── 9. AIO市場変化 ──
    md += [
        "## 8. AIO市場変化",
        "",
        "> 引用ドメインの一覧ではなく、市場で誰の存在感が変化したか（継続・新規・消失）を示します。",
        "",
    ]
    md.append(_citation_trends_md(data.get("citation_trends", {})))

    # ── 10. AIO本文変化サマリー ──
    md += [
        "## 9. AIO本文変化サマリー",
        "",
        "> 本文全文は data/aio/daily/YYYY-MM-DD.json を参照してください。ここではハッシュ変化のみを扱います。",
        "",
    ]
    md.append(_aio_text_change_summary_md(data.get("aio_text_changes", {})))

    # ── 11. サービス系 ──
    md += ["## 10. サービス系", ""]
    md.append(_page_table_md(data["service_pages"], site_url))

    # ── 12. サービスDefinition系 ──
    md += ["## 11. サービスDefinition系", ""]
    md.append(_page_table_md(data["aio_pages"], site_url))

    # ── 13. マガジン系 ──
    md += ["## 12. マガジン系", ""]
    md.append(_page_table_md(data["def_pages"], site_url))

    # ── 14. 重要ページ ──
    md += ["## 13. 重要ページ分析", ""]
    md.append(_page_table_md(data["lp_pages"], site_url))

    # ── 15. デバイス別 ──
    md += ["## 14. デバイス別分析", ""]
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
        "## 15. 参考：CTR確認候補クエリ",
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
    md += [f"## 16. 上位クエリ TOP{top_n}", ""]
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
    md += [f"## 17. 上位ページ TOP{top_n}", ""]
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
        "## 18. 新規・更新ページのインデックス監視",
        "",
        "> 新規Definition記事・更新ページがSearch Console上に出始めたかを追跡しています"
        "（config/aio_monitor.yml の new_index_watch に追加すると対象になります）。",
        "",
    ]
    idx_mon = data.get("index_monitoring", [])
    _STATUS_LABEL_MD = {
        "not_detected":       "未検出",
        "indexed":            "インデックス済（表示ごく僅か）",
        "gaining_impressions": "Impression獲得中",
        "gaining_clicks":     "クリック発生",
    }
    if idx_mon:
        md += [
            "| ページ | 公開・更新日 | 経過日数 | 初回Impression | 初回Click | Impression | クリック | 平均順位 | ステータス |",
            "|-------|-------------|---------|---------------|----------|-----------|---------|---------|----------|",
        ]
        for p in idx_mon:
            first_imp = p["first_impression_date"] or "-"
            first_clk = p["first_click_date"] or "-"
            days = f'{p["days_since_publish"]}日' if p["days_since_publish"] is not None else "-"
            status_txt = _STATUS_LABEL_MD.get(p["status"], p["status"])
            md.append(
                f"| [{p['page_title']}]({p['url']}) | {p['published_or_updated_date'] or '-'} | {days} "
                f"| {first_imp} | {first_clk} | {fi(p['current_impressions'])} | {fi(p['current_clicks'])} "
                f"| {fpos(p['current_position'])} | {status_txt} |"
            )
        md.append("")
    else:
        md.append("監視対象ページが未登録です（config/aio_monitor.yml の new_index_watch を設定してください）。\n")

    # ── 16. ページ単体クエリ分析 ──
    md += [
        "## 19. ページ単体クエリ分析",
        "",
        "> ページ単体に絞り込んだ検索クエリから、地域ページとしての認識度・AIOハブ候補としての育ちを確認します"
        "（config/aio_monitor.yml の page_query_watch に対象ページを追加できます）。",
        "",
    ]
    md.append(_page_query_watch_md(data.get("page_query_watch", [])))

    # ── 17. 次週/次月確認ポイント ──
    next_heading = "次週確認ポイント・改善推奨" if period_type == "weekly" else "次月確認ポイント・改善推奨"
    md += [f"## 20. {next_heading}", ""]
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
            return f'<span style="color:{MUTED};font-size:14px;">—</span>'
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
            suffix = "（初期値）" if not has_global_prev else ""
            found.append(f'「{top["name"]}」は {" ・ ".join(vals)} を記録している{suffix}。')

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
            suffix = "（初期値）" if not has_global_prev else ""
            found.append(f'「{name}」が順位 {cd["position"]:.1f} 位で検索上位10位以内に到達している{suffix}。')

        if not found:
            if has_global_prev:
                found.append("データ蓄積中。目立った変化は検出されなかった。")
            else:
                found.append(f"データ蓄積中。次{period_unit}以降の比較対象として初期観測フェーズの値を記録した。")
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
            if has_global_prev:
                hypos.append("観察対象となる明確な数値変化は検出されなかった。")
            else:
                hypos.append(f"観察対象となる数値変化は次{period_unit}以降に整理する。現時点では初期値として記録する。")
        return hypos[:5]

    # ── AIO対策サジェスション ──
    def gen_aio_suggestions() -> list[str]:
        gaio = data.get("google_aio", [])
        ranking, total_aio = aio_competitor_ranking(gaio, entity_types=data.get("domain_entity_types", {}))
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

        # P1: 全体状況（Search ConsoleとAIO日次観測のステータスを混同しないよう分離）
        active_areas = []
        if kw_active:
            active_areas.append("ブランディング会社・インナーブランディング領域")
        if aio_active or def_active:
            active_areas.append("サービス・Definition記事群")
        aio_summary_for_note = data.get("aio_daily_summary", {})
        if not has_global_prev:
            p1 = (
                f'<strong style="color:{NAVY};">&#9432; 初期観測フェーズ</strong> — '
                f'Search Console側は比較対象となる前{period_unit}データが存在しないため、初期観測フェーズとして扱います。'
            )
        else:
            p1 = f'Search Consoleは既存データによる{period_unit}次比較を実施。'
        if aio_summary_for_note.get("is_reference_only"):
            obs_days = aio_summary_for_note.get("observed_days", 0)
            exp_days = aio_summary_for_note.get("expected_days", 0)
            p1 += f' AIO日次観測は蓄積期間が浅く（観測{obs_days}/{exp_days}日）、AIO関連の数値は参考値です。'
        if active_areas:
            p1 += f'{"および".join(active_areas)}で検索露出が確認できている。'
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

        # P4: 次週/次月確認
        if kw_active:
            names = "・".join(f'「{r["query"]}」' for r in kw_active[:2])
            next_period = "次週以降" if period_type == "weekly" else "次月以降"
            paras.append(f'{next_period}は {names} 関連KWの推移を重点確認する。')

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

    # ── 市場存在感サマリー（レポート冒頭） ──
    _MPS_ARROW_COLOR = {"up": UP, "hold": MUTED, "down": DOWN}

    def _market_presence_summary_section() -> str:
        mps = data.get("market_presence_summary", [])
        if not mps:
            return ""
        title_html = (
            f'<tr><td style="padding-bottom:4px;">'
            f'<div style="color:{NAVY};font-size:19px;font-weight:700;">市場存在感サマリー</div>'
            f'</td></tr>'
            f'<tr><td style="padding-bottom:10px;">'
            f'<p style="color:{MUTED};font-size:12px;margin:0;">市場優先度＝市場機会の大きさ（Action Itemsの実行優先度とは別軸）。</p>'
            f'</td></tr>'
        )

        def arrow_span(direction: str) -> str:
            color = _MPS_ARROW_COLOR.get(direction, MUTED)
            arrow = _MPS_DIRECTION_ARROW.get(direction, "?")
            return f'<span style="color:{color};font-weight:700;">{arrow}</span>'

        _PRIORITY_COLOR = {"高": DOWN, "中": "#e0a930", "低": MUTED}

        hdr = (
            th("市場", "left", "150") + th("SEO状況", "left", "170") + th("AIO状況", "center", "90") +
            th("総合", "left", "90") + th("市場優先度", "center", "70") + th("コメント", "left", "220")
        )
        rows_html = ""
        for m in mps:
            seo, aio = m["seo"], m["aio"]
            if seo["status"] == "データ不足":
                seo_detail = "表示回数僅少"
            else:
                prv_txt = f"{fpos(seo['previous_position'])}位" if seo["previous_position"] is not None else "データなし"
                seo_detail = f"{fpos(seo['current_position'])}位 ← {prv_txt}"
            seo_cell = f'{esc(seo["status"])}<br><span style="color:{MUTED};font-size:13px;">{esc(seo_detail)}</span>'
            overall_cell = f'{arrow_span(m["overall"])} {esc(m["market_status"])}'
            priority_color = _PRIORITY_COLOR.get(m["market_priority"], MUTED)
            priority_cell = f'<span style="color:{priority_color};font-weight:700;">{esc(m["market_priority"])}</span>'
            rows_html += (
                "<tr>"
                + td_cell(esc(m["market"]))
                + td_cell(seo_cell)
                + td_cell(esc(aio["status"]), "center")
                + td_cell(overall_cell)
                + td_cell(priority_cell, "center")
                + td_cell(esc(m["comment"]))
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + f'<tr><td style="padding-bottom:24px;">{table_html}</td></tr>'

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

    # ── Action Items セクション（来週やること・1件=1カード表示） ──
    def _action_item_card(a: dict) -> str:
        signal_txt = "・".join(a["expected_signal"]) if a["expected_signal"] else "-"
        target_val = f'{esc(a["target"])}（{esc(a["content_type"])}）'
        review_val = f'{esc(a["review_date"])}（{a["review_weeks"]}週間後）'

        def row(label: str, value: str) -> str:
            return (
                f'<tr><td style="padding:3px 0;font-size:14px;color:{MUTED};width:90px;vertical-align:top;white-space:nowrap;">{esc(label)}</td>'
                f'<td style="padding:3px 0;font-size:14px;color:{TEXT};">{value}</td></tr>'
            )

        rows_html = (
            row("根拠", esc(a["evidence"]))
            + row("対象", target_val)
            + row("施策", esc(a["action"]))
            + row("期待シグナル", esc(signal_txt))
            + row("確認日", review_val)
        )
        return (
            f'<tr><td style="padding-bottom:12px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 8px 8px 0;padding:14px 18px;">'
            f'<div style="margin-bottom:8px;">'
            f'<span style="display:inline-block;background:{NAVY};color:#fff;font-size:12px;font-weight:700;'
            f'padding:2px 8px;border-radius:10px;margin-right:8px;">優先度{a["priority"]}</span>'
            f'<span style="color:{NAVY};font-size:16px;font-weight:700;">{esc(a["title"])}</span>'
            f'</div>'
            f'<table cellpadding="0" cellspacing="0" width="100%">{rows_html}</table>'
            f'</div></td></tr>'
        )

    def _action_items_section() -> str:
        rows = "".join(_action_item_card(a) for a in gen_action_items(data))
        return (
            f'<tr><td style="padding-top:20px;padding-bottom:2px;">'
            f'<div style="color:{NAVY};font-size:21px;font-weight:800;margin-bottom:2px;">Action Items</div>'
            f'<p style="color:{MUTED};font-size:13px;margin:0 0 12px;">来週やるべき実務タスク（実行優先度順。市場優先度＝機会の大きさとは別軸）</p>'
            f'</td></tr>'
            f'{rows}'
        )

    # ── Google AI Overview セクション（SerpAPI）──
    def _aio_competitor_status_section() -> str:
        rows = data.get("aio_competitor_status", [])
        title_html = sec_title("3", "AIO競合状況（週次AIOスナップショット）")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'今週誰が引用されたか・前週との差分（新規/消失）・TCD引用状況をまとめます。'
            f'本文全体・URL一覧は reports/google_aio/ 配下のJSONを参照してください。'
            f'</p></td></tr>'
        )
        if not rows:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'観測データなし（SERPAPI_API_KEY 未設定または無効）。</p></td></tr>'
            )

        blocks = ""
        for r in rows:
            header = (
                f'<tr><td style="padding-top:10px;padding-bottom:4px;">'
                f'<div style="color:{NAVY};font-size:16px;font-weight:700;">{esc(r["query"])}</div>'
                f'</td></tr>'
            )
            if not r["aio_exists"]:
                blocks += header + check_row("AIOは表示されていません。") + f'<tr><td style="padding-bottom:6px;"></td></tr>'
                continue
            cited = "・".join(r["cited_companies"]) or "なし"
            lines = [f"今週引用: {cited}"]
            if not r["has_previous"]:
                lines.append("前週比: 初回観測")
            else:
                new_txt = "・".join(r["new_companies"]) or "なし"
                lost_txt = "・".join(r["lost_companies"]) or "なし"
                lines.append(f"前週比: 新規 {new_txt} ／ 消失 {lost_txt}")
            tcd_color = UP if r["tcd_cited"] else MUTED
            tcd_line = f'TCD: <span style="color:{tcd_color};font-weight:700;">{"引用あり" if r["tcd_cited"] else "未引用"}</span>'
            blocks += (
                header
                + "".join(check_row(t) for t in lines)
                + f'<tr><td style="padding:4px 0 4px 12px;border-left:3px solid {BORDER};'
                f'color:{TEXT};font-size:16px;line-height:1.6;">&#8250; {tcd_line}</td></tr>'
                + f'<tr><td style="padding-bottom:6px;"></td></tr>'
            )
        return title_html + note_row + blocks

    # ── AIO競合ランキング セクション ──
    _ENTITY_TYPE_LABEL = {
        "branding_company": "ブランディング会社", "consulting_company": "コンサル会社",
        "client_company": "クライアント企業", "media": "メディア",
        "public_institution": "公的機関", "financial_institution": "金融機関",
        "platform": "プラットフォーム", "unknown": "不明",
    }

    def _ranking_table_html(items: list[dict]) -> str:
        """domain列には正規化済みホスト名のみを表示し、解決できない場合は「-」を表示する
        （会社名をdomain列へ代入しない）。"""
        hdr = (
            th("企業（表示名）", "left") + th("ドメイン", "left") + th("種別", "center", "110") +
            th("対象キーワード", "left") + th("AIO登場率", "right", "110")
        )
        rows_html = ""
        for item in items:
            kw_txt = "、".join(item.get("keywords", [])) or "—"
            share_txt = f'{item["count"]}件 ({item["appearance_rate"]:.0f}%)'
            is_tcd = item.get("is_tcd", False)
            name_cell = (
                f'<span style="color:{NAVY};font-weight:700;">{esc(item["display_name"])}</span>'
                if is_tcd else esc(item["display_name"])
            )
            entity_label = _ENTITY_TYPE_LABEL.get(item.get("entity_type", "unknown"), "不明")
            share_color = UP if item["count"] > 0 else MUTED
            share_cell = f'<span style="color:{share_color};font-weight:700;">{share_txt}</span>'
            row_style = ' style="background:#f0fdf4;"' if is_tcd else ""
            domain_txt = item.get("domain") or "-"
            rows_html += (
                f'<tr{row_style}>{td_cell(name_cell)}{td_cell(esc(domain_txt))}'
                f'{td_cell(entity_label, "center")}{td_cell(esc(kw_txt))}{td_cell(share_cell, "right")}</tr>\n'
            )
        return wrap_table(hdr, rows_html)

    def _aio_competitor_section() -> str:
        ranking, total_aio = aio_competitor_ranking(data.get("google_aio", []), entity_types=data.get("domain_entity_types", {}))
        competitors, reference_sources = split_ranking_by_entity_type(ranking)
        title_html = sec_title("4", "AIO競合ランキング")
        label = "今週" if period_type == "weekly" else "今月"
        subtitle = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:15px;margin:0 0 8px;">'
            f'{label}観測AIO: {total_aio}件</p></td></tr>'
        )
        if not ranking:
            return (
                title_html + subtitle +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'AIOに引用された企業は検出されませんでした。</p></td></tr>'
            )

        competitor_block = ""
        if competitors:
            competitor_block = f'<tr><td style="padding-bottom:8px;">{_ranking_table_html(competitors[:10])}</td></tr>'
            competitor_block += (
                f'<tr><td style="padding:4px 0 4px;">'
                f'<p style="color:{MUTED};font-size:12px;margin:0;">'
                f'※ AIO登場率 = そのドメインが引用されたAIO件数 ÷ AIO表示クエリ数。'
                f'1件のAIOに複数ドメインが引用され得るため、合計は100%になりません。'
                f'</p></td></tr>'
            )
            hypothesis = gen_aio_competitor_hypothesis(ranking, total_aio, label)
            if hypothesis:
                competitor_block += (
                    f'<tr><td style="padding:10px 0 16px;">'
                    f'<p style="color:{TEXT};font-size:15px;line-height:1.8;margin:0;">{esc(hypothesis)}</p>'
                    f'</td></tr>'
                )
        else:
            competitor_block = (
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'競合（ブランディング/コンサル会社）は検出されませんでした。</p></td></tr>'
            )

        ref_title_html = (
            f'<tr><td style="padding-top:12px;padding-bottom:6px;">'
            f'<div style="color:{NAVY};font-size:16px;font-weight:700;">AIO参考情報源</div>'
            f'<p style="color:{MUTED};font-size:13px;margin:2px 0 8px;">'
            f'銀行・行政・メディア等、ブランディング/コンサル会社以外の引用元（競合ではなく参考情報として分離）</p>'
            f'</td></tr>'
        )
        if reference_sources:
            ref_block = (
                ref_title_html +
                f'<tr><td style="padding-bottom:8px;">{_ranking_table_html(reference_sources[:10])}</td></tr>' +
                (
                    f'<tr><td style="padding-bottom:16px;">'
                    f'<p style="color:{MUTED};font-size:12px;margin:0;">'
                    f'※ 全{len(reference_sources)}件のうち上位10件を表示。</p></td></tr>'
                    if len(reference_sources) > 10 else ""
                )
            )
        else:
            ref_block = (
                ref_title_html +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">参考情報源は検出されませんでした。</p></td></tr>'
            )

        return title_html + subtitle + competitor_block + ref_block

    # ── AIO Opportunity セクション ──
    def _aio_opportunity_section() -> str:
        opp = data.get("aio_opportunity", [])
        title_html = sec_title("5", "AIO Opportunity")
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
        title_html = sec_title("6", "AIO Daily Summary（日次AIO推移）")
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
        observed_days = summary.get("observed_days", 0)
        expected_days = summary.get("expected_days", 0)
        coverage_rate = summary.get("coverage_rate", 0.0)
        stat_cells = [
            ("観測日数",          f'{observed_days} / {expected_days}日（{coverage_rate*100:.0f}%）'),
            ("観測クエリ数",      str(summary["aio_query_count"])),
            ("AIO表示率",        f'{rate*100:.0f}%' if rate is not None else "-"),
            ("TCD引用率",        f'{tcd_rate*100:.0f}%' if tcd_rate is not None else "-"),
            ("前回観測からの本文変更件数", str(summary["aio_text_changed_count"])),
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
        warning_html = ""
        if summary.get("is_reference_only"):
            warning_html = (
                f'<tr><td style="padding-top:8px;">'
                f'<div style="background:#fff7e6;border-left:4px solid #e0a930;border-radius:0 6px 6px 0;'
                f'padding:10px 16px;color:{TEXT};font-size:14px;">'
                f'&#9888; 日次観測開始直後のため（観測{observed_days}日 < {AIO_DAILY_MIN_REFERENCE_DAYS}日）、'
                f'AIO表示率・引用率は参考値です。'
                f'</div></td></tr>'
            )
        elif not summary.get("supports_weekly_comparison"):
            warning_html = (
                f'<tr><td style="padding-top:8px;">'
                f'<p style="color:{MUTED};font-size:13px;margin:0;">'
                f'&#9432; 観測日数が{AIO_DAILY_MIN_COMPARISON_DAYS}日未満のため、AIO日次データの週次比較は行っていません。'
                f'</p></td></tr>'
            )
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{stat_table}</td></tr>' + warning_html

    # ── クエリ別AIO日次モニター セクション ──
    def _aio_query_daily_section() -> str:
        aio_summary_ref = data.get("aio_daily_summary", {})
        query_level = aio_summary_ref.get("query_level", [])
        title_html = sec_title("7", "クエリ別AIO日次モニター")
        if not query_level:
            return (
                title_html +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'AIO日次データ未取得。</p></td></tr>'
            )
        # 参考値注記は表の上に一度だけ（各セルへは繰り返さない）
        note_row = ""
        if aio_summary_ref.get("is_reference_only"):
            obs = aio_summary_ref.get("observed_days", 0)
            note_row = (
                f'<tr><td style="padding-bottom:8px;">'
                f'<p style="color:{MUTED};font-size:13px;margin:0;">'
                f'※ 本表は観測{obs}日分の基準データです。表示率・引用率は参考値として扱います。'
                f'</p></td></tr>'
            )
        hdr = (
            th("クエリ", "left", "150") +
            th("観測日数", "right", "70") +
            th("AIO表示日数", "right", "90") +
            th("AIO表示率", "right", "90") +
            th("TCD引用日数", "right", "90") +
            th("TCD言及日数", "right", "90") +
            th("最新ステータス", "center", "100") +
            th("前回からの本文変更回数", "right", "90")
        )
        _STATUS_LABEL = {
            "OK_WITH_AIO": ("AIOあり", UP), "OK_NO_AIO": ("AIOなし", MUTED),
            "FETCH_ERROR": ("取得失敗", DOWN), "API_KEY_MISSING": ("APIキー未設定", DOWN),
        }
        rows_html = ""
        for r in query_level:
            rate = f"{r['aio_exists_rate']*100:.0f}%" if r["aio_exists_rate"] is not None else "-"
            observed_days = r.get("observed_days", 0)
            s_label, s_color = _STATUS_LABEL.get(r["latest_status"], (r["latest_status"], MUTED))
            rows_html += (
                "<tr>"
                + td_cell(esc(r["query"]))
                + td_cell(str(observed_days), "right")
                + td_cell(str(r["aio_exists_days"]), "right")
                + td_cell(rate, "right")
                + td_cell(str(r["tcd_cited_days"]), "right")
                + td_cell(str(r["tcd_mentioned_days"]), "right")
                + td_cell(f'<span style="color:{s_color};font-weight:700;">{s_label}</span>', "center")
                + td_cell(str(r["aio_text_changed_count"]), "right")
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── AIO市場変化 セクション ──
    def _citation_trends_player_table_html(rows: list[dict], label: str, color: str) -> str:
        header_row = (
            f'<tr><td style="padding-top:6px;padding-bottom:4px;">'
            f'<span style="color:{color};font-size:14px;font-weight:700;">{esc(label)}</span></td></tr>'
        )
        if not rows:
            return header_row + (
                f'<tr><td style="padding-bottom:10px;">'
                f'<p style="color:{MUTED};font-size:14px;margin:0;">該当なし</p></td></tr>'
            )
        hdr = th("ドメイン", "left", "160") + th("引用回数", "right", "70") + th("対象クエリ")
        rows_html = ""
        for r in rows:
            queries = "・".join(esc(q) for q in r["related_queries"][:3]) or "-"
            rows_html += (
                "<tr>" + td_cell(esc(r["domain"])) + td_cell(str(r["cited_count"]), "right")
                + td_cell(f'<span style="font-size:14px;">{queries}</span>') + "</tr>\n"
            )
        return header_row + f'<tr><td style="padding-bottom:10px;">{wrap_table(hdr, rows_html)}</td></tr>'

    def _aio_domain_trend_section() -> str:
        ct = data.get("citation_trends", {})
        title_html = sec_title("8", "AIO市場変化")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'引用ドメインの一覧ではなく、市場で誰の存在感が変化したか（継続・新規・消失）を示します。'
            f'</p></td></tr>'
        )
        summary_lines = ct.get("trend_summary", [])
        if ct.get("entity_note"):
            summary_lines = summary_lines + [ct["entity_note"]]
        if ct.get("weekly_snapshot_note"):
            summary_lines = summary_lines + [ct["weekly_snapshot_note"]]
        summary_html = "".join(check_row(s) for s in summary_lines)

        # 日次AIO推移データが未取得の週は、継続/新規/消失の3表を空欄で並べても情報がないため省略する
        if ct.get("no_current_observation"):
            players_html = ""
        else:
            players_html = (
                _citation_trends_player_table_html(ct.get("stable_players", []), "継続引用（安定した存在感）", MUTED)
                + _citation_trends_player_table_html(ct.get("emerging_players", []), "新規引用（存在感が高まった可能性）", UP)
                + _citation_trends_player_table_html(ct.get("disappeared_players", []), "引用消失（前週から存在感が低下）", DOWN)
            )
        return title_html + note_row + summary_html + players_html + f'<tr><td style="padding-bottom:12px;"></td></tr>'

    # ── AIO本文変化サマリー セクション ──
    def _aio_text_change_section() -> str:
        changes = data.get("aio_text_changes", {})
        title_html = sec_title("9", "AIO本文変化サマリー")
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
        title_html = sec_title("15", "新規・更新ページのインデックス監視")
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
            "not_detected":        ("未検出", MUTED),
            "indexed":             ("インデックス済（表示ごく僅か）", MUTED),
            "gaining_impressions": ("Impression獲得中", "#e0a930"),
            "gaining_clicks":      ("クリック発生", UP),
        }
        hdr = (
            th("ページ", "left", "160") +
            th("公開・更新日", "center", "90") +
            th("経過日数", "center", "60") +
            th("初回Imp", "center", "90") +
            th("初回Click", "center", "90") +
            th("Impression", "right", "70") + th("Click", "right", "55") +
            th("平均順位", "right", "65") +
            th("ステータス", "center", "130")
        )
        rows_html = ""
        for p in idx_mon:
            s_label, s_color = _STATUS_LABEL_HTML.get(p["status"], (p["status"], MUTED))
            first_imp = esc(p["first_impression_date"] or "-")
            first_clk = esc(p["first_click_date"] or "-")
            days = f'{p["days_since_publish"]}日' if p["days_since_publish"] is not None else "-"
            rows_html += (
                "<tr>"
                + td_cell(f'<a href="{esc(p["url"])}" style="color:{NAVY};text-decoration:none;">{esc(p["page_title"])}</a>')
                + td_cell(esc(p["published_or_updated_date"] or "-"), "center")
                + td_cell(days, "center")
                + td_cell(first_imp, "center")
                + td_cell(first_clk, "center")
                + td_cell(fi(p["current_impressions"]), "right")
                + td_cell(fi(p["current_clicks"]), "right")
                + td_cell(fpos(p["current_position"]), "right")
                + td_cell(f'<span style="color:{s_color};font-weight:700;">{s_label}</span>', "center")
                + "</tr>\n"
            )
        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── ページ単体クエリ分析 セクション（地域ページ判定・AIOハブ候補判定） ──
    def _page_query_watch_section() -> str:
        pqw = data.get("page_query_watch", [])
        title_html = sec_title("16", "ページ単体クエリ分析")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:14px;margin:0;">'
            f'ページ単体に絞り込んだ検索クエリから、地域ページとしての認識度・AIOハブ候補としての育ちを確認します'
            f'（config/aio_monitor.yml の page_query_watch に対象ページを追加できます）。'
            f'</p></td></tr>'
        )
        if not pqw:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:15px;margin:0;">'
                f'監視対象ページが未設定です。</p></td></tr>'
            )

        _REG_COLOR = {"confirmed": UP, "strengthening": UP, "weak": "#e0a930", "none": MUTED}
        _HUB_COLOR = {"none": MUTED, "initial_signal": "#e0a930", "growing": UP, "established": UP}

        blocks = ""
        for analysis in pqw:
            label = analysis["label"]
            tc = analysis["totals_current"]
            tp = analysis["totals_previous"]
            reg = analysis["regional"]
            hub = analysis["aio_hub"]
            ref_s, ref_e = analysis["reference_period"]

            page_title = (
                f'<tr><td style="padding-top:14px;padding-bottom:6px;">'
                f'<div style="color:{NAVY};font-size:16px;font-weight:700;">{esc(label)}単体の検索クエリ</div>'
                f'<p style="color:{MUTED};font-size:12px;margin:2px 0 0;">'
                f'{esc(analysis["url"])}<br>参考期間（90日目安）: {ref_s} 〜 {ref_e}（クエリ{len(analysis["ref_rows"])}件）'
                f'</p></td></tr>'
            )

            totals_hdr = th("指標", "left", "120") + th("今週", "right", "90") + th("前週", "right", "90")
            totals_rows = "".join([
                "<tr>" + td_cell("クエリ数") + td_cell(str(tc["query_count"]), "right") + td_cell(str(tp["query_count"]), "right") + "</tr>",
                "<tr>" + td_cell("クリック合計") + td_cell(fi(tc["clicks"]), "right") + td_cell(fi(tp["clicks"]), "right") + "</tr>",
                "<tr>" + td_cell("表示回数合計") + td_cell(fi(tc["impressions"]), "right") + td_cell(fi(tp["impressions"]), "right") + "</tr>",
                "<tr>" + td_cell("平均CTR") + td_cell(fp(tc["ctr"]), "right") + td_cell(fp(tp["ctr"]), "right") + "</tr>",
                "<tr>" + td_cell("平均順位") + td_cell(fpos(tc["position"]), "right") + td_cell(fpos(tp["position"]), "right") + "</tr>",
            ])
            totals_table = (
                f'<tr><td style="padding-bottom:10px;">{wrap_table(totals_hdr, totals_rows)}</td></tr>'
            )

            reg_color = _REG_COLOR.get(reg["status"], MUTED)
            reg_lines = [
                f'大阪・関西を含むクエリ数: {reg["query_count"]}件（表示回数 {fi(reg["impressions"])}、前週 {fi(reg["prv_impressions"])}）',
                f'参考90日での地域クエリ数: {reg["ref_query_count"]}件',
            ]
            if reg["new_queries"]:
                reg_lines.append(f'前週からの新規出現クエリ: {"・".join(reg["new_queries"])}')
            if reg["moved_queries"]:
                moved = "・".join(f'{r["query"]}({fpos_chg(r["position_diff"])})' for r in reg["moved_queries"])
                reg_lines.append(f'順位が大きく動いたクエリ（±3位以上）: {moved}')
            reg_block = (
                f'<tr><td style="padding-top:8px;padding-bottom:4px;">'
                f'<span style="color:{NAVY};font-size:14px;font-weight:700;">地域ページとしての認識: </span>'
                f'<span style="color:{reg_color};font-size:14px;font-weight:700;">{_PQ_REGIONAL_STATUS_LABEL.get(reg["status"], reg["status"])}</span>'
                f'</td></tr>'
                + "".join(check_row(t) for t in reg_lines)
            )

            hub_color = _HUB_COLOR.get(hub["status"], MUTED)
            hub_lines = [
                f'一般語: {len(hub["general_rows"])}件 / 比較・選定系: {len(hub["comparison_rows"])}件 / 自然文: {len(hub["natural_language_rows"])}件',
            ]
            if hub["new_queries"]:
                hub_lines.append(f'前週からの新規出現クエリ: {"・".join(hub["new_queries"])}')
            hub_block = (
                f'<tr><td style="padding-top:8px;padding-bottom:4px;">'
                f'<span style="color:{NAVY};font-size:14px;font-weight:700;">AIOハブ候補としての広がり: </span>'
                f'<span style="color:{hub_color};font-size:14px;font-weight:700;">{_PQ_AIO_HUB_STATUS_LABEL.get(hub["status"], hub["status"])}</span>'
                f'</td></tr>'
                + "".join(check_row(t) for t in hub_lines)
                + (
                    f'<tr><td style="padding:4px 0 8px;">'
                    f'<p style="color:{MUTED};font-size:12px;margin:0;">'
                    f'※ 自然文・比較クエリの出現はAI Overviewへの掲載を意味しません。会話型検索に近い検索意図が生まれている兆候として参考情報にとどめてください。'
                    f'</p></td></tr>'
                )
            )

            recs_block = (
                f'<tr><td style="padding-top:8px;padding-bottom:4px;">'
                f'<span style="color:{NAVY};font-size:14px;font-weight:700;">次の対応</span></td></tr>'
                + "".join(check_row(t) for t in _page_query_recommendations(analysis))
                + f'<tr><td style="padding-bottom:8px;"></td></tr>'
            )

            blocks += page_title + totals_table + reg_block + hub_block + recs_block

        return title_html + note_row + blocks

    market_presence_summary_html = _market_presence_summary_section()
    executive_summary_html = _executive_summary_section()
    ai_insights_html = _ai_insights_section()
    action_items_html = _action_items_section()
    aio_competitor_status_html = _aio_competitor_status_section()
    aio_competitor_html = _aio_competitor_section()
    aio_opportunity_html = _aio_opportunity_section()
    aio_daily_summary_html = _aio_daily_summary_section()
    aio_query_daily_html = _aio_query_daily_section()
    aio_domain_trend_html = _aio_domain_trend_section()
    aio_text_change_html = _aio_text_change_section()
    index_monitoring_html = _index_monitoring_section()
    page_query_watch_html = _page_query_watch_section()

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

<!-- 市場存在感サマリー -->
{market_presence_summary_html}

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

<!-- 3. AIO競合状況 -->
{aio_competitor_status_html}

<!-- 4. AIO競合ランキング -->
{aio_competitor_html}

<!-- 5. AIO Opportunity -->
{aio_opportunity_html}

<!-- 6. AIO Daily Summary -->
{aio_daily_summary_html}

<!-- 7. クエリ別AIO日次モニター -->
{aio_query_daily_html}

<!-- 8. AIO市場変化 -->
{aio_domain_trend_html}

<!-- 9. AIO本文変化サマリー -->
{aio_text_change_html}

<!-- 10. 重点キーワード監視 -->
{sec_title("10", "重点キーワード監視")}
<tr><td style="padding-bottom:4px;">{kw_table_html}</td></tr>
{kw_note}

<!-- 11. サービス系 -->
{sec_title("11", "サービス系")}
<tr><td style="padding-bottom:4px;">{svc_table_html}</td></tr>
{svc_note}

<!-- 12. サービスDefinition系 -->
{sec_title("12", "サービスDefinition系")}
<tr><td style="padding-bottom:4px;">{aio_table_html}</td></tr>
{aio_note}

<!-- 13. マガジン系 -->
{sec_title("13", "マガジン系")}
<tr><td style="padding-bottom:4px;">{def_table_html}</td></tr>
{def_note}

<!-- 14. 重要ページ監視 -->
{sec_title("14", "重要ページ監視")}
<tr><td style="padding-bottom:4px;"><p style="color:{MUTED};font-size:14px;margin:0 0 8px 0;">Search Console上の検索流入評価。実際のCV数ではありません（CV計測は将来GA4で実施）。</p></td></tr>
<tr><td style="padding-bottom:4px;">{lp_table_html}</td></tr>
{lp_note}

<!-- 15. 新規・更新ページのインデックス監視 -->
{index_monitoring_html}

<!-- 16. ページ単体クエリ分析 -->
{page_query_watch_html}

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
  <a href="https://datastudio.google.com/reporting/4d67c760-d3dc-4a1e-b2d2-47ce3b44f69a/page/p_w254n6vx5d/edit"
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
        aio_competitor_status_data = build_aio_competitor_status(
            google_aio_data, gaio_previous, config.get("market_presence_queries", []),
            domain_aliases=config.get("domain_aliases", {}),
        )
    else:
        google_aio_data = []
        point_watch_data = []
        aio_competitor_status_data = []
        gaio_previous = None

    # ページ単体クエリ分析（地域ページ判定・AIOハブ候補判定）
    page_query_watch_data: list[dict] = []
    pqw_cfg = config.get("page_query_watch", {})
    if pqw_cfg.get("pages"):
        print("ページ単体クエリ分析中...")
        cls_cfg = pqw_cfg.get("classification", {})
        regional_terms = cls_cfg.get("regional_terms", [])
        comparison_terms = cls_cfg.get("comparison_terms", [])
        nl_min_length = cls_cfg.get("natural_language_min_length", 20)
        ref_end_dt = datetime.strptime(cur_end, "%Y-%m-%d")
        ref_start_dt = ref_end_dt - timedelta(days=pqw_cfg.get("reference_days", 90) - 1)
        ref_start_str = ref_start_dt.strftime("%Y-%m-%d")

        gsc_dir = Path("data/gsc")
        gsc_dir.mkdir(parents=True, exist_ok=True)

        for page_cfg in pqw_cfg["pages"]:
            page_url = page_cfg["url"]
            label = page_cfg.get("label", page_url)
            print(f"  {label} のクエリ取得中...")

            def _page_query_df(s: str, e: str) -> pd.DataFrame:
                rows = _fetch_rows(
                    service, site_url, s, e, ["query"],
                    dimension_filter={"dimension": "page", "operator": "equals", "expression": page_url},
                )
                return _to_df(rows, ["query"])

            cur_page_df = _page_query_df(cur_start, cur_end)
            prv_page_df = _page_query_df(prv_start, prv_end)
            ref_page_df = _page_query_df(ref_start_str, cur_end)

            analysis = pqa.analyze_page_query(
                cur_page_df, prv_page_df, ref_page_df,
                regional_terms, comparison_terms, nl_min_length,
            )
            analysis["label"] = label
            analysis["url"] = page_url
            analysis["reference_period"] = (ref_start_str, cur_end)
            page_query_watch_data.append(analysis)

            slug = page_cfg.get("slug", "page")
            pd.DataFrame(pqa.build_period_records(analysis)).to_csv(
                gsc_dir / f"{slug}_page_queries.csv", index=False, encoding="utf-8-sig"
            )
            pd.DataFrame(pqa.build_comparison_records(analysis)).to_csv(
                gsc_dir / f"{slug}_page_queries_weekly_comparison.csv", index=False, encoding="utf-8-sig"
            )

    # AIO日次モニター（aio_daily_monitor.py）データ読み込み
    print("AIO日次モニターデータ読み込み中...")
    aio_daily_df = load_aio_daily_csv()
    aio_domains_df = load_aio_domains_daily_csv()
    aio_daily_summary = aggregate_aio_daily(aio_daily_df, cur_start, cur_end)
    aio_domain_trend = aggregate_aio_domain_trend(aio_domains_df, cur_start, cur_end, prv_start, prv_end)
    _weekly_snapshot_ranking, _ = aio_competitor_ranking(google_aio_data, entity_types=config.get("domain_entity_types", {}))
    _weekly_snapshot_competitors, _ = split_ranking_by_entity_type(_weekly_snapshot_ranking)
    citation_trends = build_citation_trends(aio_domain_trend, config.get("domain_entity_types", {}), _weekly_snapshot_competitors)
    aio_text_changes = aggregate_aio_text_changes(aio_daily_df, cur_start, cur_end)
    print(f"  集計対象期間: {cur_start} 〜 {cur_end}")
    print(f"  AIO表示件数: {aio_daily_summary['aio_exists_count']} / TCD引用件数: {aio_daily_summary['tcd_cited_count']}")

    # 集計・分析
    print("データ集計中...")
    key_kws_data = kw_compare(cur_queries, prv_queries, config.get("key_keywords", []))
    market_presence_summary = build_market_presence_summary(
        key_kws_data, google_aio_data, gaio_previous, config.get("market_presence_queries", []),
    )
    data = {
        "periods": ((cur_start, cur_end), (prv_start, prv_end)),
        "totals": {
            "current": calc_totals(cur_pages),
            "previous": calc_totals(prv_pages),
        },
        "key_kws": key_kws_data,
        "market_presence_summary": market_presence_summary,
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
        "aio_competitor_status": aio_competitor_status_data,
        "aio_opportunity":  compute_aio_opportunity(google_aio_data, cur_queries),
        "index_monitoring": compute_index_monitoring(
            config.get("new_index_watch", []) or [], cur_pages, prv_pages, site_url,
            service=service, cur_end=cur_end,
        ),
        "aio_daily_summary": aio_daily_summary,
        "aio_domain_trend":  aio_domain_trend,
        "citation_trends":   citation_trends,
        "aio_text_changes":  aio_text_changes,
        "domain_entity_types": config.get("domain_entity_types", {}),
        "period_type":    actual_period,
        "page_query_watch": page_query_watch_data,
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
