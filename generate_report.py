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


def fetch_page_top_queries(svc, site: str, s: str, e: str, page_url: str, n: int = 5) -> list[str]:
    """指定ページの上位クエリを impressions 順で返す。"""
    variants = {page_url, page_url.rstrip("/"), page_url.rstrip("/") + "/"}
    for url in variants:
        rows = _fetch_rows(
            svc, site, s, e,
            dimensions=["query"],
            dimension_filter={"dimension": "page", "operator": "equals", "expression": url},
            row_limit=n * 3,
        )
        if rows:
            df = _to_df(rows, ["query"])
            if not df.empty:
                return df.nlargest(n, "impressions")["query"].tolist()
    return []


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


def fetch_google_aio(
    queries: list[str],
    aio_cfg: dict | None = None,
    output_dir: str = "reports/google_aio",
    force: bool = False,
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

            # フォールバック1: root に page_token がある場合
            if not ai_ov and res.get("ai_overview_page_token"):
                aio_res = GoogleSearch({
                    "engine":     "google_ai_overview",
                    "page_token": res["ai_overview_page_token"],
                    "api_key":    api_key,
                }).get_dict()
                ai_ov = aio_res.get("ai_overview")

            # フォールバック2: ai_ov が存在するが中身が page_token のみ（コンテンツ未展開）
            if ai_ov and not ai_ov.get("text_blocks") and not ai_ov.get("references") and ai_ov.get("page_token"):
                aio_res = GoogleSearch({
                    "engine":     "google_ai_overview",
                    "page_token": ai_ov["page_token"],
                    "api_key":    api_key,
                }).get_dict()
                ai_ov = aio_res.get("ai_overview") or ai_ov

            # AIO が存在するが詳細コンテンツを取得できなかった場合を判定
            _detail_fetch_failed = (
                ai_ov is not None
                and not ai_ov.get("text_blocks")
                and not ai_ov.get("references")
                and not ai_ov.get("text")
                and "page_token" in ai_ov
            )
            if _detail_fetch_failed:
                aio_exists = True
                status     = "AIO_DETAIL_FETCH_FAILED"

            if ai_ov and not _detail_fetch_failed:
                aio_exists = True
                status     = "OK_WITH_AIO"
                # text_blocks 形式（新）と text 形式（旧）の両対応
                if ai_ov.get("text_blocks"):
                    aio_text = " ".join(
                        b.get("snippet", "") for b in ai_ov["text_blocks"]
                        if isinstance(b, dict) and b.get("type") == "paragraph"
                    )
                else:
                    aio_text = ai_ov.get("text", "")

                for ref in ai_ov.get("references", []):
                    if isinstance(ref, str):
                        url, domain, title = ref, _extract_domain(ref), ""
                    else:
                        # SerpAPI: URL フィールドは "link"（旧 "url" にも対応）
                        url    = ref.get("link", "") or ref.get("url", "")
                        src    = ref.get("source", {})
                        # source は文字列またはdictどちらの場合もある
                        if isinstance(src, dict):
                            domain = src.get("name", "") or _extract_domain(url)
                        else:
                            domain = str(src) if src else _extract_domain(url)
                        title  = ref.get("title", "")
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


def load_previous_aio_cache(output_dir: str, today_str: str) -> list[dict]:
    """当日以外で最新のAIOキャッシュを返す。"""
    import json
    out_dir = Path(output_dir)
    if not out_dir.exists():
        return []
    for f in sorted(out_dir.glob("*.json"), reverse=True):
        if f.stem != today_str:
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
    return []


def compute_aio_diff(current: list[dict], previous: list[dict]) -> dict[str, dict]:
    """クエリ別のAIO変化ステータスを返す。
    status: newly_appeared | disappeared | tcd_newly_cited | tcd_lost_citation |
            citation_domains_increased | citation_domains_decreased | no_change | no_previous_data
    """
    prv_map = {r["query"]: r for r in previous}
    result: dict[str, dict] = {}
    for r in current:
        q = r["query"]
        p = prv_map.get(q)
        if p is None:
            result[q] = {"status": "no_previous_data"}
            continue
        cur_aio   = bool(r.get("aio_exists"))
        prv_aio   = bool(p.get("aio_exists"))
        cur_cited = r.get("tcd_cited", False)
        prv_cited = p.get("tcd_cited", False)
        cur_doms  = len(set(r.get("cited_domains", [])))
        prv_doms  = len(set(p.get("cited_domains", [])))
        if cur_aio and not prv_aio:
            result[q] = {"status": "newly_appeared"}
        elif not cur_aio and prv_aio:
            result[q] = {"status": "disappeared"}
        elif cur_aio and prv_aio:
            if cur_cited and not prv_cited:
                result[q] = {"status": "tcd_newly_cited"}
            elif not cur_cited and prv_cited:
                result[q] = {"status": "tcd_lost_citation"}
            elif cur_doms > prv_doms:
                result[q] = {"status": "citation_domains_increased", "delta": cur_doms - prv_doms}
            elif cur_doms < prv_doms:
                result[q] = {"status": "citation_domains_decreased", "delta": prv_doms - cur_doms}
            else:
                result[q] = {"status": "no_change"}
        else:
            result[q] = {"status": "no_change"}
    return result


def fetch_cited_page_features(url: str, timeout: int = 10) -> dict:
    """引用URLのページ構造を解析して「なぜ引用されたか」の手がかりを返す。"""
    import requests
    from bs4 import BeautifulSoup

    base: dict = {
        "url": url, "ok": False, "error": None,
        "title": "", "h1": "", "h2s": [], "meta_desc": "",
        "schema_types": [], "content_type": "",
        "has_faq": False, "has_numbered_list": False,
        "word_count": 0,
    }
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; TCD-SEO-Bot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        title_tag = soup.find("title")
        base["title"] = title_tag.get_text(strip=True) if title_tag else ""

        h1_tag = soup.find("h1")
        base["h1"] = h1_tag.get_text(strip=True)[:100] if h1_tag else ""

        base["h2s"] = [h.get_text(strip=True)[:80] for h in soup.find_all("h2")][:6]

        meta = soup.find("meta", attrs={"name": "description"})
        base["meta_desc"] = meta.get("content", "")[:200] if meta else ""

        # JSON-LD スキーマ抽出
        schema_types: list[str] = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                d = _json.loads(script.string or "")
                def _collect_types(obj):
                    if isinstance(obj, dict):
                        if "@type" in obj:
                            t = obj["@type"]
                            schema_types.extend(t if isinstance(t, list) else [t])
                        for v in obj.values():
                            _collect_types(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            _collect_types(item)
                _collect_types(d)
            except Exception:
                pass
        base["schema_types"] = list(dict.fromkeys(schema_types))
        base["has_faq"] = any("FAQ" in t or "faq" in t.lower() for t in base["schema_types"])

        base["has_numbered_list"] = bool(soup.find("ol"))

        body_text = soup.get_text(separator=" ", strip=True)
        base["word_count"] = len(body_text)

        # コンテンツタイプ判定
        title_h1 = (base["title"] + " " + base["h1"]).lower()
        h2_text  = " ".join(base["h2s"]).lower()
        if base["has_faq"] or any("?" in h or "か？" in h or "ですか" in h for h in base["h2s"]):
            base["content_type"] = "FAQ型"
        elif any(w in title_h1 for w in ["とは", "わかりやすく", "意味", "定義"]):
            base["content_type"] = "定義型"
        elif any(w in title_h1 for w in ["おすすめ", "選", "比較", "一覧", "ランキング"]):
            base["content_type"] = "リスト比較型"
        elif any(w in title_h1 for w in ["方法", "やり方", "手順", "ステップ", "進め方"]):
            base["content_type"] = "ハウツー型"
        else:
            base["content_type"] = "その他"

        base["ok"] = True
    except Exception as e:
        base["error"] = str(e)[:120]
    return base


def analyze_aio_citations(
    aio_results: list[dict],
    output_dir: str = "reports/citation_analysis",
    force: bool = False,
) -> dict[str, list[dict]]:
    """引用URLのページ構造を解析してクエリ別に返す。同日キャッシュを再利用。"""
    import json
    from datetime import date

    today_str  = date.today().strftime("%Y-%m-%d")
    out_dir    = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / f"{today_str}.json"

    if cache_path.exists() and not force:
        print(f"  引用分析キャッシュ使用: {cache_path}")
        return json.loads(cache_path.read_text(encoding="utf-8"))

    _TCD = ["tcd.jp", "株式会社tcd"]
    result: dict[str, list[dict]] = {}

    for r in aio_results:
        if not r.get("cited_urls"):
            continue
        query = r["query"]
        comp_urls = [u for u in r["cited_urls"] if not any(p in u.lower() for p in _TCD)][:3]
        if not comp_urls:
            continue
        print(f"  競合コンテンツ分析中: 「{query}」({len(comp_urls)}件)...")
        result[query] = []
        for url in comp_urls:
            feats = fetch_cited_page_features(url)
            result[query].append(feats)

    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  引用分析結果保存: {cache_path}")
    return result


# 共通トピック判定キーワード（競合分析・TCD差分で共用）
_TOPIC_KEYWORDS: list[tuple[str, str]] = [
    ("比較",      "比較一覧"),
    ("選び方",    "選び方・選定基準"),
    ("費用",      "費用・料金相場"),
    ("事例",      "導入事例・実績"),
    ("メリット",  "メリット・効果"),
    ("方法",      "進め方・手順"),
    ("とは",      "定義・概要"),
    ("FAQ",       "FAQ・よくある質問"),
    ("ランキング","ランキング・おすすめ"),
    ("会社",      "会社・企業一覧"),
]


def analyze_common_elements(pages: list[dict]) -> dict:
    """競合ページの共通要素を機械的に抽出する。"""
    from collections import Counter
    ok = [p for p in pages if p.get("ok")]
    if not ok:
        return {}
    n = len(ok)

    type_counts = Counter(p.get("content_type", "その他") for p in ok)
    dominant    = type_counts.most_common(1)[0][0]

    faq_count  = sum(1 for p in ok if p.get("has_faq"))
    list_count = sum(1 for p in ok if p.get("has_numbered_list"))

    schema_counter = Counter(s for p in ok for s in p.get("schema_types", []))
    common_schemas = [s for s, c in schema_counter.items() if c >= max(2, n - 1)]

    common_topics: list[dict] = []
    for kw, label in _TOPIC_KEYWORDS:
        hits = sum(
            1 for p in ok
            if any(kw in h for h in p.get("h2s", []))
            or kw in (p.get("title", "") + " " + p.get("h1", ""))
        )
        if hits >= 2:
            common_topics.append({"keyword": kw, "label": label, "count": hits, "total": n})

    wcs    = [p["word_count"] for p in ok if p.get("word_count", 0) > 0]
    avg_wc = int(sum(wcs) / len(wcs)) if wcs else 0

    return {
        "dominant_content_type": dominant,
        "content_type_counts":   dict(type_counts),
        "faq_count":     faq_count,
        "faq_ratio":     f"{faq_count}/{n}",
        "list_count":    list_count,
        "list_ratio":    f"{list_count}/{n}",
        "common_schemas": common_schemas[:6],
        "common_topics":  common_topics,
        "avg_word_count": avg_wc,
        "page_count":     n,
    }


def compute_tcd_gap(common: dict, tcd: dict) -> dict:
    """共通要素とTCDページを照合して実装済み・未実装を返す。"""
    tcd_text = " ".join([
        tcd.get("title", ""),
        tcd.get("h1", ""),
        " ".join(tcd.get("h2s", [])),
        tcd.get("meta_desc", ""),
    ]).lower()

    implemented:     list[str] = []
    not_implemented: list[str] = []

    for topic in common.get("common_topics", []):
        kw = topic["keyword"]
        if kw.lower() in tcd_text:
            implemented.append(topic["label"])
        else:
            not_implemented.append(topic["label"])

    if common.get("faq_count", 0) >= 2:
        label = "FAQ・よくある質問"
        if label not in implemented and label not in not_implemented:
            (implemented if tcd.get("has_faq") else not_implemented).append(label)

    return {
        "implemented":      implemented,
        "not_implemented":  not_implemented,
        "tcd_word_count":   tcd.get("word_count", 0),
        "tcd_content_type": tcd.get("content_type", "不明"),
        "tcd_title":        tcd.get("title", "")[:80],
    }


def build_citation_insights(
    citation_analysis: dict[str, list[dict]],
    tcd_url: str = "https://tcd.jp/",
    output_dir: str = "reports/citation_insights",
    force: bool = False,
) -> dict[str, dict]:
    """共通要素・TCD差分を機械的に集計して返す。insight フィールドは Claude Code が別途書き込む。"""
    import json as _json
    from datetime import date
    out_dir    = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    today_str  = date.today().strftime("%Y-%m-%d")
    cache_path = out_dir / f"{today_str}.json"

    if cache_path.exists() and not force:
        print(f"  インサイトキャッシュ使用: {cache_path}")
        return _json.loads(cache_path.read_text(encoding="utf-8"))

    # TCDトップページ解析（別キャッシュ）
    tcd_cache = out_dir / "tcd_page.json"
    if tcd_cache.exists() and not force:
        tcd_features = _json.loads(tcd_cache.read_text(encoding="utf-8"))
    else:
        print(f"  TCDページ解析中: {tcd_url}...")
        tcd_features = fetch_cited_page_features(tcd_url)
        tcd_cache.write_text(_json.dumps(tcd_features, ensure_ascii=False, indent=2), encoding="utf-8")

    result: dict[str, dict] = {}
    for query, pages in citation_analysis.items():
        print(f"  機械分析中: 「{query}」...")
        common = analyze_common_elements(pages)
        gap    = compute_tcd_gap(common, tcd_features)
        result[query] = {"common_elements": common, "tcd_gap": gap, "insight": {}}

    cache_path.write_text(_json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  インサイト保存: {cache_path}")
    print(f"  ※ GPT考察は Claude Code で追記: {cache_path}")
    return result


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


def gen_actionable_findings(data: dict, has_prev: bool) -> list[dict]:
    """施策価値の高い発見を {discovery, insight, action} 構造で返す。
    優先順位: 順位10〜20位 > CTR異常値 > 急上昇 > Imp上位（フォールバック）
    """
    all_items = (
        [(r, r.get("query", ""), "kw")  for r in data["key_kws"]] +
        [(r, r.get("name",  ""), "svc") for r in data["service_pages"]] +
        [(r, r.get("name",  ""), "aio") for r in data["aio_pages"]] +
        [(r, r.get("name",  ""), "def") for r in data["def_pages"]]
    )
    findings: list[dict] = []
    seen: set[str] = set()

    def _add(f: dict, key: str) -> bool:
        if key in seen or len(findings) >= 4:
            return False
        seen.add(key)
        findings.append(f)
        return True

    # Priority 1: 順位10〜20位（ランクアップ改善余地）
    for r, name, kind in sorted(
        [(r, n, k) for r, n, k in all_items
         if 10 <= r["cur"]["position"] <= 20 and r["cur"]["impressions"] >= MIN_IMP_FINDING],
        key=lambda x: -x[0]["cur"]["impressions"],
    )[:2]:
        cd = r["cur"]
        _add({
            "discovery": f'「{name}」が {fi(cd["impressions"])} Imp・順位 {cd["position"]:.0f} 位',
            "insight":   f'順位10〜20位はクリック獲得の改善余地が大きいゾーン。現在CTR {fp(cd["ctr"])} にとどまっている',
            "action":    "内部リンク強化・関連記事追加・FAQ追加によるトップ10入りを狙う",
        }, name)

    # Priority 2: CTR異常値（上位順位なのに期待CTRの50%未満）
    for r, name, kind in all_items:
        cd = r["cur"]
        if cd["impressions"] < MIN_IMP_AIO or cd["position"] <= 0 or cd["position"] > 10:
            continue
        pos_int  = max(1, min(10, round(cd["position"])))
        expected = EXPECTED_CTR_BY_POS.get(pos_int, 0.022)
        if cd["ctr"] < expected * 0.5:
            _add({
                "discovery": f'「{name}」が順位 {cd["position"]:.0f} 位・{fi(cd["impressions"])} Imp に対してCTR {fp(cd["ctr"])}',
                "insight":   f'期待CTR {expected*100:.1f}% の実現率が {cd["ctr"]/expected*100:.0f}% にとどまっている。タイトル・snippetの訴求が弱い可能性',
                "action":    "titleタグ・metaDescriptionの見直し。Definition構造の再設計を検討",
            }, name + "_ctr")

    # Priority 3: 急上昇（前週比 3位以上改善）
    if has_prev:
        for r, name, kind in sorted(
            [(r, n, k) for r, n, k in all_items
             if r.get("pos_chg") is not None
             and r["pos_chg"] < -RANK_CHG_MIN
             and r["cur"]["impressions"] >= MIN_IMP_FINDING],
            key=lambda x: x[0]["pos_chg"],
        )[:1]:
            cd = r["cur"]
            _add({
                "discovery": f'「{name}」の順位が {abs(r["pos_chg"]):.0f} 位改善（{r["prv"]["position"]:.0f}位→{cd["position"]:.0f}位）',
                "insight":   "順位改善の勢いがある。この機会にさらなる上位定着を狙える",
                "action":    "関連キーワードでの内部リンク追加・コンテンツ補強で上位定着を図る",
            }, name + "_rise")

    # Fallback: Imp上位
    if not findings:
        for r, name, kind in sorted(all_items, key=lambda x: -x[0]["cur"]["impressions"])[:2]:
            cd = r["cur"]
            if cd["impressions"] < MIN_IMP_FINDING:
                continue
            _add({
                "discovery": f'「{name}」が {fi(cd["impressions"])} Imp・順位 {cd["position"]:.0f} 位',
                "insight":   "最も検索露出が多い項目。今後のデータ蓄積のベースラインとなる",
                "action":    "コンテンツ品質維持・内部リンク整備を継続する",
            }, name)

    if not findings:
        findings.append({
            "discovery": "データ蓄積中",
            "insight":   "次週以降の比較対象としてベースラインを記録した",
            "action":    "引き続きデータ蓄積を継続する",
        })
    return findings


def gen_actionable_issues(data: dict, has_prev: bool) -> list[dict]:
    """課題と施策案を {issue, probable_cause, recommended_action} 構造で返す。
    自動判定: rank_over_20 | rank_under_10_and_ctr_low | impressions_high_and_click_zero
    """
    all_items = (
        [(r, r.get("query", ""), "kw")  for r in data["key_kws"]] +
        [(r, r.get("name",  ""), "svc") for r in data["service_pages"]] +
        [(r, r.get("name",  ""), "aio") for r in data["aio_pages"]] +
        [(r, r.get("name",  ""), "def") for r in data["def_pages"]]
    )
    issues: list[dict] = []
    seen: set[str] = set()

    def _add(issue: dict, key: str) -> bool:
        if key in seen or len(issues) >= 4:
            return False
        seen.add(key)
        issues.append(issue)
        return True

    # Rule 1: 順位20位以上 → 順位課題
    for r, name, kind in sorted(
        [(r, n, k) for r, n, k in all_items
         if r["cur"]["position"] > 20 and r["cur"]["impressions"] >= MIN_IMP_FINDING],
        key=lambda x: -x[0]["cur"]["impressions"],
    )[:2]:
        cd = r["cur"]
        _add({
            "issue":              f'順位課題：「{name}」が {cd["position"]:.0f} 位（{fi(cd["impressions"])} Imp）',
            "probable_cause":     "内部リンクの不足・コンテンツ網羅性・被リンク不足の可能性",
            "recommended_action": "内部リンク追加 / 関連記事追加 / FAQ追加 / 構造化マークアップ改善",
        }, name + "_rank")

    # Rule 2: 順位10位以内・CTR低 → CTR課題
    for r, name, kind in all_items:
        cd = r["cur"]
        if cd["impressions"] < MIN_IMP_AIO or cd["position"] <= 0 or cd["position"] > 10:
            continue
        pos_int  = max(1, min(10, round(cd["position"])))
        expected = EXPECTED_CTR_BY_POS.get(pos_int, 0.022)
        if cd["ctr"] < expected * 0.5:
            _add({
                "issue":              f'CTR課題：「{name}」が順位 {cd["position"]:.0f} 位でCTR {fp(cd["ctr"])}（期待値 {expected*100:.1f}%）',
                "probable_cause":     "タイトルの訴求力不足・メタディスクリプション最適化余地・AI Overview によるCTR吸収の可能性",
                "recommended_action": "titleタグ改善 / description改善 / Definition構造の再設計",
            }, name + "_ctr")

    # Rule 3: Imp高・クリックゼロ → 検索結果訴求不足
    for r, name, kind in sorted(
        [(r, n, k) for r, n, k in all_items
         if r["cur"]["impressions"] >= MIN_IMP_FINDING and r["cur"]["clicks"] == 0],
        key=lambda x: -x[0]["cur"]["impressions"],
    )[:1]:
        cd = r["cur"]
        _add({
            "issue":              f'検索結果訴求不足：「{name}」が {fi(cd["impressions"])} Imp に対してクリックゼロ',
            "probable_cause":     "検索意図とコンテンツのズレ・タイトル訴求不足・または順位が低すぎる",
            "recommended_action": "タイトル改善 / 概要文改善 / 検索意図に合わせたコンテンツ見直し",
        }, name + "_noimp")

    if not issues:
        issues.append({
            "issue":              "明確な課題は現時点では検出なし",
            "probable_cause":     "データ蓄積が少ない、または全体的に安定している状態",
            "recommended_action": "引き続きデータ蓄積を継続し、来週以降の推移を確認する",
        })
    return issues


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
            _fetch_failed = r.get("status") == "AIO_DETAIL_FETCH_FAILED"
            tcd_c = "判定不能" if _fetch_failed else ("✅ あり" if r.get("tcd_cited") else "なし")
            tcd_m = "判定不能" if _fetch_failed else ("✅" if r.get("tcd_mentioned") else "なし")
            comp  = "・".join(r.get("competitor_domains", [])[:3]) or ("-" if not _fetch_failed else "判定不能")
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

    # ── 3. サービス系 → 4 ──
    md += ["## 3. サービス系", ""]
    md.append(_page_table_md(data["service_pages"], site_url))

    # ── 4. サービスDefinition系 ──
    md += ["## 4. サービスDefinition系", ""]
    md.append(_page_table_md(data["aio_pages"], site_url))

    # ── 5. マガジン系 ──
    md += ["## 5. マガジン系", ""]
    md.append(_page_table_md(data["def_pages"], site_url))

    # ── 6. 重要ページ ──
    md += ["## 6. 重要ページ分析", ""]
    md.append(_page_table_md(data["lp_pages"], site_url))

    # ── 7. デバイス別 ──
    md += ["## 7. デバイス別分析", ""]
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

    # ── 7. CTR確認候補クエリ（参考） ──
    md += [
        "## 8. 参考：CTR確認候補クエリ",
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

    # ── 9. 上位クエリ ──
    md += [f"## 9. 上位クエリ TOP{top_n}", ""]
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

    # ── 10. 上位ページ ──
    md += [f"## 10. 上位ページ TOP{top_n}", ""]
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

    # ── 11. 次月確認ポイント ──
    md += ["## 11. 次月確認ポイント・改善推奨", ""]
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

    def chg_span(val: float | None) -> str:
        if val is None:
            return f'<span style="color:{MUTED};font-size:11px;">NEW</span>'
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
            f'<div style="color:{MUTED};font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;">{esc(num)}</div>'
            f'<div style="color:{NAVY};font-size:15px;font-weight:700;margin-top:2px;">{esc(title)}</div>'
            f'</td></tr></table></td></tr>'
        )

    def info_box(html: str) -> str:
        return (
            f'<tr><td style="padding-bottom:16px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 6px 6px 0;'
            f'padding:16px 20px;color:{TEXT};font-size:14px;line-height:1.8;">{html}</div></td></tr>'
        )

    def finding_card(d: dict) -> str:
        disc = esc(d.get("discovery", ""))
        data_txt = esc(d.get("supporting_data", d.get("insight", "")))
        body = (
            f'<div style="font-size:12px;color:{MUTED};padding-left:8px;border-left:2px solid {BORDER};">{data_txt}</div>'
        ) if data_txt else ""
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 6px 6px 0;padding:10px 16px;">'
            f'<div style="font-size:13px;color:{TEXT};margin-bottom:{("6px" if data_txt else "0")};font-weight:500;">&#128269; {disc}</div>'
            f'{body}'
            f'</div></td></tr>'
        )

    def issue_card(d: dict) -> str:
        iss  = esc(d.get("issue",              ""))
        caus = esc(d.get("probable_cause",     ""))
        rec  = esc(d.get("recommended_action", ""))
        return (
            f'<tr><td style="padding-bottom:10px;">'
            f'<div style="background:{HYPO_BG};border-left:4px solid {DOWN};border-radius:0 6px 6px 0;padding:12px 16px;">'
            f'<div style="font-size:11px;font-weight:700;color:{DOWN};letter-spacing:.5px;margin-bottom:4px;text-transform:uppercase;">課題</div>'
            f'<div style="font-size:13px;color:{TEXT};margin-bottom:8px;">&#9888; {iss}</div>'
            f'<div style="font-size:11px;font-weight:700;color:{MUTED};letter-spacing:.5px;margin-bottom:3px;text-transform:uppercase;">推定要因</div>'
            f'<div style="font-size:12px;color:{TEXT};margin-bottom:8px;padding-left:8px;border-left:2px solid {BORDER};">{caus}</div>'
            f'<div style="font-size:11px;font-weight:700;color:{NAVY};letter-spacing:.5px;margin-bottom:3px;text-transform:uppercase;">推奨施策</div>'
            f'<div style="font-size:12px;color:{NAVY};font-weight:600;padding-left:8px;border-left:2px solid {CYAN};">&#10148; {rec}</div>'
            f'</div></td></tr>'
        )

    def check_row(text: str) -> str:
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="padding:4px 0 4px 12px;border-left:3px solid {BORDER};'
            f'color:{TEXT};font-size:13px;line-height:1.6;">&#8250; {esc(text)}</div></td></tr>'
        )

    def th(label: str, align: str = "left", w: str = "") -> str:
        ws = f' width="{w}"' if w else ""
        return (
            f'<th{ws} style="background:{NAVY};color:#fff;font-size:11px;font-weight:600;'
            f'padding:8px 10px;text-align:{align};white-space:nowrap;letter-spacing:.3px;">{esc(label)}</th>'
        )

    def td_cell(content: str, align: str = "left") -> str:
        return (
            f'<td style="padding:7px 10px;font-size:12px;color:{TEXT};'
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
            f'<p style="color:{MUTED};font-size:11px;margin:0;">'
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
                f'<tr><td colspan="{col_count}" style="padding:9px 12px;font-size:13px;'
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
                ref_span  = f'<span style="color:{MUTED};font-size:11px;">参考値</span>'

                # クリック前月比: 前期クリック < 3 は参考値
                cl_cell = td_cell(
                    ref_span if r["prv"]["clicks"] < 3 else chg_span(r["click_chg"]),
                    "right"
                )
                # 順位変化: 前期なし → 比較対象なし / 現期Imp < 30 → 参考値
                if not has_prev:
                    pos_cell = td_cell(f'<span style="color:{MUTED};font-size:11px;">比較対象なし</span>', "right")
                elif cd["impressions"] < MIN_IMP_FINDING:
                    pos_cell = td_cell(ref_span, "right")
                else:
                    pos_cell = td_cell(pos_span(r["pos_chg"]), "right")

                data_row += cl_cell + pos_cell
            rows += data_row + "</tr>\n"
        return wrap_table(hdr, rows)

    # ── LP監視テーブル（上位クエリ付き） ──
    def lp_table_with_queries(items: list[dict], name_fn) -> str:
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
                f'<tr><td colspan="{col_count}" style="padding:9px 12px;font-size:13px;'
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
                has_prev2 = r["prv"]["impressions"] > 0
                ref_span  = f'<span style="color:{MUTED};font-size:11px;">参考値</span>'
                cl_cell   = td_cell(
                    ref_span if r["prv"]["clicks"] < 3 else chg_span(r["click_chg"]), "right"
                )
                if not has_prev2:
                    pos_cell = td_cell(f'<span style="color:{MUTED};font-size:11px;">比較対象なし</span>', "right")
                elif cd["impressions"] < MIN_IMP_FINDING:
                    pos_cell = td_cell(ref_span, "right")
                else:
                    pos_cell = td_cell(pos_span(r["pos_chg"]), "right")
                data_row += cl_cell + pos_cell
            rows += data_row + "</tr>\n"
            top_qs = r.get("top_queries", [])
            if top_qs:
                tags = "　".join(
                    f'<span style="background:#e8f4fd;border-radius:3px;padding:2px 6px;'
                    f'font-size:11px;color:{NAVY};">{esc(q)}</span>'
                    for q in top_qs
                )
                rows += (
                    f'<tr><td colspan="{col_count}" style="padding:5px 12px 9px;'
                    f'font-size:11px;color:{MUTED};border-bottom:1px solid {BORDER};">'
                    f'&#128269; 流入クエリ TOP5: {tags}</td></tr>\n'
                )
        return wrap_table(hdr, rows)

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
            f'<strong style="color:{NAVY};">&#9432; ベースライン月</strong> — '
            f'Search Consoleの計測開始が{report_month}中旬のため、本レポートは初期ベースラインとして扱います。'
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

    findings_html    = "".join(finding_card(f) for f in gen_actionable_findings(data, has_global_prev))
    hypothesis_html  = "".join(issue_card(i)  for i in gen_actionable_issues(data, has_global_prev))
    checkpoints_html = "".join(check_row(c)   for c in gen_html_checkpoints())

    # ── Google AI Overview セクション（SerpAPI）──
    def _google_aio_section() -> str:
        gaio     = data.get("google_aio", [])
        aio_diff = data.get("aio_diff", {})
        title_html = sec_title("3", "Google AI Overview 観測")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:11px;margin:0;">'
            f'SerpAPI 経由で AI Overview の有無・TCD引用・競争状況を観測。引用数の変化とTCD引用獲得状況を時系列で追跡します。'
            f'</p></td></tr>'
        )
        no_real_data = all(r.get("status") == "API_KEY_MISSING" for r in gaio) if gaio else True
        if not gaio or no_real_data:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:12px;margin:0;">'
                f'観測データなし（SERPAPI_API_KEY 未設定または無効）。</p></td></tr>'
            )

        ORANGE = "#f59e0b"

        # ── 今週のAIO所見（機械集計）──
        aio_count  = sum(1 for r in gaio if r.get("aio_exists"))
        tcd_cited_list = [r["query"] for r in gaio if r.get("tcd_cited")]
        new_cited  = [q for q, d in aio_diff.items() if d.get("status") == "tcd_newly_cited"]
        lost_cited = [q for q, d in aio_diff.items() if d.get("status") == "tcd_lost_citation"]
        # 競合引用ドメイン集計（全クエリ合計）
        from collections import Counter as _Counter
        comp_dom_counter: _Counter = _Counter()
        for r in gaio:
            if r.get("aio_exists") and r.get("status") != "AIO_DETAIL_FETCH_FAILED":
                real_u = [u for u in r.get("cited_urls", []) if "google.com/searchviewer" not in u]
                for u in real_u:
                    d = _extract_domain(u)
                    if not any(p in d for p in ["tcd.jp"]):
                        comp_dom_counter[d] += 1
        top_comp = [d for d, _ in comp_dom_counter.most_common(3)]

        obs_items = []
        obs_items.append(
            f'{len(gaio)}クエリ中 {aio_count}クエリでAIOが出現'
        )
        if tcd_cited_list:
            obs_items.append(f'TCD引用あり: {" / ".join(tcd_cited_list)}')
        else:
            obs_items.append('TCDはいずれのクエリでも引用されていない')
        if top_comp:
            obs_items.append(f'引用競合上位: {" / ".join(top_comp)}')
        if new_cited:
            obs_items.append(f'新規引用獲得: {" / ".join(new_cited)}')
        if lost_cited:
            obs_items.append(f'引用消失: {" / ".join(lost_cited)}')
        if not new_cited and not lost_cited:
            obs_items.append('前週から引用状況に大きな変化なし')

        obs_html = "".join(
            f'<li style="font-size:12px;color:{TEXT};margin-bottom:4px;">{esc(item)}</li>'
            for item in obs_items
        )
        summary_row = (
            f'<tr><td style="padding-bottom:12px;">'
            f'<div style="background:{INFO_BG};border-left:3px solid {CYAN};border-radius:0 6px 6px 0;padding:10px 14px;">'
            f'<div style="font-size:11px;font-weight:700;color:{CYAN};letter-spacing:.5px;margin-bottom:6px;text-transform:uppercase;">今週のAIO所見</div>'
            f'<ul style="margin:0;padding-left:16px;">{obs_html}</ul>'
            f'</div></td></tr>'
        )

        # ── テーブル（クエリ | TCD | 前週比）──
        hdr = (
            th("クエリ",  "left",   "180") +
            th("TCD",    "center", "80")  +
            th("引用数", "center", "60")  +
            th("前週比", "center", "100")
        )
        rows_html = ""
        for r in gaio:
            status       = r.get("status", "OK_NO_AIO")
            aio_exists   = r.get("aio_exists", False)
            fetch_failed = (status == "AIO_DETAIL_FETCH_FAILED")

            # TCD列
            if not aio_exists:
                tcd_txt   = "AIOなし"
                tcd_color = MUTED
            elif fetch_failed:
                tcd_txt   = "判定不能"
                tcd_color = ORANGE
            elif r.get("tcd_cited"):
                tcd_txt   = "引用あり"
                tcd_color = UP
            else:
                tcd_txt   = "引用なし"
                tcd_color = MUTED

            # 前週比列
            diff_info   = aio_diff.get(r["query"], {})
            diff_status = diff_info.get("status", "no_previous_data")
            if fetch_failed:
                diff_label, diff_color = "判定不能", ORANGE
            elif diff_status == "tcd_newly_cited":
                diff_label, diff_color = "新規引用", UP
            elif diff_status == "tcd_lost_citation":
                diff_label, diff_color = "引用消失", DOWN
            elif diff_status == "no_change" and r.get("tcd_cited"):
                diff_label, diff_color = "引用継続", UP
            else:
                diff_label, diff_color = "変化なし", MUTED

            # 引用数（Google searchviewer除く実URL）
            real_urls_count = [
                u for u in r.get("cited_urls", [])
                if "google.com/searchviewer" not in u
            ]
            if not aio_exists:
                cite_count_txt   = "－"
                cite_count_color = MUTED
            elif fetch_failed:
                cite_count_txt   = "取得失敗"
                cite_count_color = ORANGE
            else:
                cite_count_txt   = str(len(real_urls_count)) + "件"
                cite_count_color = TEXT

            rows_html += (
                "<tr>"
                + td_cell(esc(r["query"]))
                + td_cell(f'<span style="color:{tcd_color};font-weight:700;">{tcd_txt}</span>', "center")
                + td_cell(f'<span style="color:{cite_count_color};font-weight:700;">{cite_count_txt}</span>', "center")
                + td_cell(f'<span style="color:{diff_color};font-weight:700;">{diff_label}</span>', "center")
                + "</tr>\n"
            )

            # 2行目: TCD引用URL
            if not fetch_failed and r.get("tcd_cited") and r.get("tcd_cited_urls"):
                tcd_url_html = "　".join(
                    f'<a href="{esc(u)}" style="color:{UP};font-size:11px;text-decoration:none;">'
                    f'&#9989; {esc(_extract_domain(u))}</a>'
                    for u in r["tcd_cited_urls"][:2]
                )
                rows_html += (
                    f'<tr style="background:#f0fdf4;"><td colspan="4" '
                    f'style="padding:4px 12px 6px;font-size:11px;">'
                    f'<span style="color:{UP};font-weight:700;">TCD引用URL:</span> {tcd_url_html}</td></tr>\n'
                )

            # 2行目: 競合引用URL（TCD除外・Google除外・上位3件）
            real_urls = [
                u for u in r.get("cited_urls", [])
                if "google.com/searchviewer" not in u
            ]
            comp_urls = [
                u for u in real_urls
                if not any(p in u.lower() for p in ["tcd.jp", "株式会社tcd"])
            ][:3]
            if not fetch_failed and aio_exists and comp_urls:
                comp_links = "　".join(
                    f'<a href="{esc(u)}" style="color:{CYAN};font-size:11px;text-decoration:none;">'
                    f'{esc(_extract_domain(u))}</a>'
                    for u in comp_urls
                )
                rows_html += (
                    f'<tr style="background:#f8fafc;"><td colspan="4" '
                    f'style="padding:4px 12px 6px;font-size:11px;color:{MUTED};">'
                    f'&#128279; 競合引用: {comp_links}</td></tr>\n'
                )
            elif fetch_failed:
                rows_html += (
                    f'<tr style="background:#fffbeb;"><td colspan="4" '
                    f'style="padding:4px 12px 6px;font-size:11px;color:{ORANGE};">'
                    f'&#9888; AIOあり・引用詳細取得失敗。TCD引用の有無は判定できません。</td></tr>\n'
                )

        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + summary_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    # ── AIO引用競合コンテンツ分析セクション ──
    def _citation_analysis_section() -> str:
        ca       = data.get("citation_analysis", {})
        insights = data.get("citation_insights", {})
        if not ca and not insights:
            return ""

        CT_COLOR = {
            "FAQ型":      "#7c3aed",
            "定義型":     "#0369a1",
            "リスト比較型":"#0f766e",
            "ハウツー型":  "#c2410c",
            "その他":     MUTED,
        }
        PRI_COLOR = {"A": DOWN, "B": "#f59e0b", "C": MUTED}
        title_html = sec_title("3.5", "AIO引用競合コンテンツ分析")
        note_row = (
            f'<tr><td style="padding-bottom:10px;">'
            f'<p style="color:{MUTED};font-size:11px;margin:0;">'
            f'引用競合の共通要素・TCDとの差分・AIO施策優先順位を自動生成します。'
            f'</p></td></tr>'
        )
        body = ""

        # ── クエリ別ブロック ──
        all_queries = set(list(ca.keys()) + list(insights.keys()))
        for query in all_queries:
            pages   = ca.get(query, [])
            insight = insights.get(query, {})
            common  = insight.get("common_elements", {})
            gap     = insight.get("tcd_gap", {})
            gpt     = insight.get("insight", {})

            body += (
                f'<tr><td style="padding:16px 0 8px;">'
                f'<div style="font-size:14px;font-weight:700;color:{NAVY};">&#128269; {esc(query)}</div>'
                f'</td></tr>\n'
            )

            # ── 3列グリッド: 共通要素 / TCD差分 / GPT考察 ──
            # 共通要素
            topic_rows = "".join(
                f'<div style="font-size:11px;margin-bottom:3px;">&#10003; {esc(t["label"])}'
                f'<span style="color:{MUTED};font-size:10px;"> {t["count"]}/{t["total"]}社</span></div>'
                for t in common.get("common_topics", [])
            ) or f'<div style="font-size:11px;color:{MUTED};">データ不足</div>'
            ct_label = esc(common.get("dominant_content_type", "－"))
            faq_str  = common.get("faq_ratio", "－")
            wc_str   = f'約{common.get("avg_word_count",0)//1000}k字' if common.get("avg_word_count") else "－"

            col_common = (
                f'<div style="background:{INFO_BG};border-radius:6px;padding:12px 14px;height:100%;box-sizing:border-box;">'
                f'<div style="font-size:11px;font-weight:700;color:{CYAN};letter-spacing:.5px;margin-bottom:8px;text-transform:uppercase;">競合共通要素</div>'
                f'<div style="font-size:11px;color:{MUTED};margin-bottom:6px;">'
                f'タイプ: <strong style="color:{TEXT};">{ct_label}</strong>　'
                f'FAQ: <strong style="color:{TEXT};">{faq_str}</strong>　'
                f'文字数: <strong style="color:{TEXT};">{wc_str}</strong></div>'
                f'{topic_rows}'
                f'</div>'
            )

            # TCD差分
            impl_rows = "".join(
                f'<div style="font-size:11px;margin-bottom:3px;color:{UP};">&#10003; {esc(x)}</div>'
                for x in gap.get("implemented", [])
            ) or f'<div style="font-size:11px;color:{MUTED};">なし</div>'
            nimpl_rows = "".join(
                f'<div style="font-size:11px;margin-bottom:3px;color:{DOWN};">&#10005; {esc(x)}</div>'
                for x in gap.get("not_implemented", [])
            ) or f'<div style="font-size:11px;color:{MUTED};">なし</div>'
            tcd_wc = gap.get("tcd_word_count", 0)
            tcd_wc_str = f'約{tcd_wc//1000}k字' if tcd_wc else "－"

            col_gap = (
                f'<div style="background:{HYPO_BG};border-radius:6px;padding:12px 14px;height:100%;box-sizing:border-box;">'
                f'<div style="font-size:11px;font-weight:700;color:{NAVY};letter-spacing:.5px;margin-bottom:8px;text-transform:uppercase;">TCDとの差分</div>'
                f'<div style="font-size:10px;font-weight:700;color:{UP};margin-bottom:4px;letter-spacing:.3px;">実装済み</div>'
                f'{impl_rows}'
                f'<div style="font-size:10px;font-weight:700;color:{DOWN};margin-top:8px;margin-bottom:4px;letter-spacing:.3px;">未実装</div>'
                f'{nimpl_rows}'
                f'<div style="font-size:10px;color:{MUTED};margin-top:8px;">TCD文字数: {tcd_wc_str}</div>'
                f'</div>'
            )

            body += (
                f'<tr><td style="padding-bottom:4px;">'
                f'<table width="100%" cellpadding="0" cellspacing="6"><tr>'
                f'<td width="50%" valign="top">{col_common}</td>'
                f'<td width="50%" valign="top">{col_gap}</td>'
                f'</tr></table></td></tr>\n'
            )

            # ── 競合ページ詳細（折りたたみ風・小さめ） ──
            if pages:
                details = ""
                for i, p in enumerate(pages, 1):
                    ct     = p.get("content_type", "その他")
                    ct_col = CT_COLOR.get(ct, MUTED)
                    ct_badge = (
                        f'<span style="background:{ct_col};color:#fff;font-size:9px;font-weight:700;'
                        f'padding:1px 6px;border-radius:8px;margin-left:5px;">{esc(ct)}</span>'
                    )
                    dom   = _extract_domain(p["url"])
                    ttl   = esc(p.get("title","")[:60]) or esc(dom)
                    h2s   = "　".join(esc(h[:30]) for h in p.get("h2s",[])[:3])
                    wc    = p.get("word_count", 0)
                    wctxt = f'{wc//1000}k字' if wc else "－"
                    sch   = " ".join(
                        f'<span style="background:#e0f2fe;color:#0369a1;font-size:9px;padding:1px 5px;border-radius:6px;">{esc(s)}</span>'
                        for s in p.get("schema_types",[])[:3]
                    )
                    ok_flag = p.get("ok", False)
                    err_txt = f'<span style="color:{DOWN};font-size:10px;">取得失敗</span>' if not ok_flag else ""
                    h2_div = (f'<div style="font-size:10px;color:{TEXT};margin-top:3px;">H2: {h2s}</div>') if h2s and ok_flag else ""
                    details += (
                        f'<div style="border-left:2px solid {ct_col};padding:6px 10px;margin-bottom:6px;background:#fafafa;">'
                        f'<div style="font-size:11px;font-weight:700;">'
                        f'<a href="{esc(p["url"])}" style="color:{NAVY};text-decoration:none;">#{i} {ttl}</a>'
                        f'{ct_badge}{err_txt}</div>'
                        f'<div style="font-size:10px;color:{MUTED};margin-top:2px;">{esc(dom)}　文字数:{wctxt}　{sch}</div>'
                        f'{h2_div}'
                        f'</div>'
                    )
                body += (
                    f'<tr><td style="padding-bottom:16px;">'
                    f'<div style="font-size:10px;font-weight:700;color:{MUTED};letter-spacing:.5px;margin-bottom:6px;text-transform:uppercase;">'
                    f'競合ページ詳細（上位{len(pages)}件）</div>'
                    f'{details}</td></tr>\n'
                )

        return title_html + note_row + body

    citation_analysis_html = _citation_analysis_section()

    google_aio_html = _google_aio_section()

    def _director_section() -> str:
        import json as _json
        from datetime import date as _date
        ORANGE = "#f59e0b"
        dir_path = Path("reports/aio_director") / f"{_date.today().strftime('%Y-%m-%d')}.json"
        if not dir_path.exists():
            return (
                sec_title("10", "AIOディレクター判断") +
                f'<tr><td style="padding-bottom:16px;">'
                f'<div style="background:{HYPO_BG};border-left:4px solid {MUTED};border-radius:0 6px 6px 0;padding:14px 18px;color:{MUTED};font-size:12px;">'
                f'Claude Code による判断待ち。レポート生成後に別途入力してください。'
                f'</div></td></tr>'
            )
        d = _json.loads(dir_path.read_text(encoding="utf-8"))

        def _bullet_list(items: list, color: str = TEXT) -> str:
            return "".join(
                f'<li style="font-size:12px;color:{color};margin-bottom:4px;">{esc(str(x))}</li>'
                for x in items
            )

        # 現状
        state_html = ""
        cs = d.get("current_state", {})
        if cs:
            issues = cs.get("主要課題", cs.get("issues", []))
            watch  = cs.get("優先監視", cs.get("watch", []))
            if issues:
                state_html += f'<div style="font-size:11px;font-weight:700;color:{DOWN};margin-bottom:4px;">主要課題</div><ul style="margin:0 0 8px 0;padding-left:16px;">{_bullet_list(issues, DOWN)}</ul>'
            if watch:
                state_html += f'<div style="font-size:11px;font-weight:700;color:{MUTED};margin-bottom:4px;">優先監視</div><ul style="margin:0 0 0 0;padding-left:16px;">{_bullet_list(watch)}</ul>'

        # 判断
        dec = d.get("decisions", {})
        dec_html = ""
        for label, color in [("やる", UP), ("やらない", MUTED), ("保留", ORANGE)]:
            items = dec.get(label, [])
            if items:
                dec_html += f'<div style="font-size:11px;font-weight:700;color:{color};margin-bottom:3px;">{label}</div><ul style="margin:0 0 8px 0;padding-left:16px;">{_bullet_list(items)}</ul>'

        # 次アクション
        acts = d.get("next_actions", {})
        act_html = ""
        for label in ["今週実施", "来週確認", "長期施策"]:
            items = acts.get(label, [])
            if items:
                act_color = UP if label == "今週実施" else (MUTED if label == "長期施策" else TEXT)
                act_html += f'<div style="font-size:11px;font-weight:700;color:{act_color};margin-bottom:3px;">{label}</div><ul style="margin:0 0 8px 0;padding-left:16px;">{_bullet_list(items)}</ul>'

        # リスク
        risks = d.get("risks", [])
        risk_html = ""
        if risks:
            risk_html = f'<div style="font-size:11px;font-weight:700;color:{ORANGE};margin-bottom:4px;">リスク</div><ul style="margin:0;padding-left:16px;">{_bullet_list(risks, ORANGE)}</ul>'

        def _card(title: str, content: str, bg: str = "#fff", border: str = BORDER) -> str:
            return (
                f'<td valign="top"><div style="background:{bg};border:1px solid {border};border-radius:6px;padding:12px 14px;height:100%;box-sizing:border-box;">'
                f'<div style="font-size:11px;font-weight:700;color:{NAVY};letter-spacing:.5px;margin-bottom:8px;text-transform:uppercase;">{esc(title)}</div>'
                f'{content}'
                f'</div></td>'
            )

        row_html = (
            f'<tr><td style="padding-bottom:16px;">'
            f'<table width="100%" cellpadding="0" cellspacing="6"><tr>'
            + _card("現状", state_html or f'<span style="color:{MUTED};font-size:11px;">記載なし</span>', HYPO_BG)
            + _card("判断", dec_html  or f'<span style="color:{MUTED};font-size:11px;">記載なし</span>')
            + _card("次アクション", act_html or f'<span style="color:{MUTED};font-size:11px;">記載なし</span>', INFO_BG)
            + _card("リスク", risk_html or f'<span style="color:{MUTED};font-size:11px;">記載なし</span>')
            + f'</tr></table></td></tr>'
        )
        return sec_title("10", "AIOディレクター判断") + row_html

    director_html = _director_section()

    kw_table_html  = two_row_table(kw_active,  kw_name)
    svc_table_html = two_row_table(svc_active, page_name)
    aio_table_html = two_row_table(aio_active, page_name)
    def_table_html = two_row_table(def_active, page_name)
    lp_table_html  = lp_table_with_queries(lp_active, page_name)

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
<table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#fff;border-radius:10px;border:1px solid {BORDER};overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,.07);">

<!-- HEADER -->
<tr><td style="background:{NAVY};padding:36px 40px;">
  <div style="color:{CYAN};font-size:10px;font-weight:700;letter-spacing:3px;text-transform:uppercase;margin-bottom:14px;">{header_label}</div>
  <h1 style="color:#fff;font-size:21px;font-weight:700;margin:0 0 18px;line-height:1.35;letter-spacing:-.3px;">{report_title}</h1>
  <table cellpadding="0" cellspacing="0"><tr>
    <td style="padding-right:28px;">
      <div style="color:{CYAN};font-size:10px;font-weight:700;letter-spacing:1px;margin-bottom:4px;">{cur_label}</div>
      <div style="color:#cbd5e1;font-size:12px;">{cs} 〜 {ce}</div>
    </td>
    <td>
      <div style="color:#64748b;font-size:10px;font-weight:700;letter-spacing:1px;margin-bottom:4px;">{prv_label}</div>
      <div style="color:#64748b;font-size:12px;">{ps} 〜 {pe}</div>
    </td>
  </tr></table>
</td></tr>

<!-- BODY -->
<tr><td style="padding:8px 40px 40px;">
<table width="100%" cellpadding="0" cellspacing="0">

<!-- 1. 総評 -->
<tr><td style="padding-top:32px;padding-bottom:4px;">
  <div style="color:{NAVY};font-size:15px;font-weight:700;margin-bottom:10px;">1. 総評</div>
</td></tr>
{info_box(summary_html())}

<!-- 2. 今週/月の発見と対策 -->
{sec_title("2", f"今{period_unit}の発見と対策")}
{findings_html}

<!-- 3. Google AI Overview 観測 -->
{google_aio_html}

<!-- 3.5 AIO引用競合コンテンツ分析 -->
{citation_analysis_html}

<!-- 4. 重点キーワード監視 -->
{sec_title("4", "重点キーワード監視")}
<tr><td style="padding-bottom:4px;">{kw_table_html}</td></tr>
{kw_note}

<!-- 5. サービス系 -->
{sec_title("5", "サービス系")}
<tr><td style="padding-bottom:4px;">{svc_table_html}</td></tr>
{svc_note}

<!-- 6. サービスDefinition系 -->
{sec_title("6", "サービスDefinition系")}
<tr><td style="padding-bottom:4px;">{aio_table_html}</td></tr>
{aio_note}

<!-- 7. マガジン系 -->
{sec_title("7", "マガジン系")}
<tr><td style="padding-bottom:4px;">{def_table_html}</td></tr>
{def_note}

<!-- 8. 重要ページ監視 -->
{sec_title("8", "重要ページ監視")}
<tr><td style="padding-bottom:4px;"><p style="color:{MUTED};font-size:11px;margin:0 0 8px 0;">Search Console上の検索流入評価。実際のCV数ではありません（CV計測は将来GA4で実施）。</p></td></tr>
<tr><td style="padding-bottom:4px;">{lp_table_html}</td></tr>
{lp_note}

<!-- 9. 課題と施策案 -->
{sec_title("9", "課題と施策案")}
{hypothesis_html}

<!-- 10. AIOディレクター判断 -->
{director_html}

<!-- 11. 次週/月確認ポイント -->
{sec_title("11", f"次{period_unit}確認ポイント")}
{checkpoints_html}

</table>
</td></tr>

<!-- Data Studio リンク -->
<tr><td style="padding:40px 40px;text-align:center;border-top:1px solid {BORDER};">
  <a href="https://datastudio.google.com/reporting/4d67c760-d3dc-4a1e-b2d2-47ce3b44f69a"
     style="display:inline-block;padding:9px 24px;background:{NAVY};color:#fff;
            font-size:13px;font-weight:600;text-decoration:none;border-radius:5px;letter-spacing:.3px;">
    Data Studio で確認する →
  </a>
</td></tr>

<!-- FOOTER -->
<tr><td style="background:{BG};border-top:1px solid {BORDER};padding:18px 40px;text-align:center;">
  <p style="color:{MUTED};font-size:11px;margin:0;line-height:1.7;">
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
    aio_output_dir = str(Path(args.output_dir) / "google_aio")
    if aio_cfg.get("enabled", False):
        print("Google AI Overview を観測中（SerpAPI）...")
        google_aio_data = fetch_google_aio(
            queries    = aio_cfg.get("queries", []),
            aio_cfg    = aio_cfg,
            output_dir = aio_output_dir,
            force      = args.force_aio,
        )
    else:
        google_aio_data = []

    # AIO前週比較
    from datetime import date as _date
    _today_str = _date.today().strftime("%Y-%m-%d")
    _previous_aio = load_previous_aio_cache(aio_output_dir, _today_str)
    aio_diff_data = compute_aio_diff(google_aio_data, _previous_aio) if google_aio_data and _previous_aio else {}


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
        "google_aio":     google_aio_data,
        "aio_diff":       aio_diff_data,
        "period_type":    actual_period,
    }

    # AIO引用競合コンテンツ分析
    print("AIO引用競合コンテンツ分析中...")
    data["citation_analysis"] = analyze_aio_citations(
        google_aio_data,
        output_dir = str(Path(args.output_dir) / "citation_analysis"),
        force      = args.force_aio,
    )

    # 共通要素・TCD差分（機械分析）
    print("競合インサイト生成中...")
    data["citation_insights"] = build_citation_insights(
        data["citation_analysis"],
        tcd_url    = config["site_url"],
        output_dir = str(Path(args.output_dir) / "citation_insights"),
        force      = args.force_aio,
    )

    # LP上位クエリ取得
    print("LP上位クエリ取得中...")
    for lp in data["lp_pages"]:
        lp["top_queries"] = fetch_page_top_queries(service, site_url, cur_start, cur_end, lp["url"])

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
