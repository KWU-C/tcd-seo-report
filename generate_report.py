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


def fetch_google_aio(
    queries: list[str],
    headless: bool = True,
    wait_ms: int = 2500,
    output_dir: str = "reports/aio_visibility",
) -> list[dict]:
    """Playwright でGoogle検索し、AI Overview の有無・TCD言及・引用URLを観測する。
    当日キャッシュが存在する場合はそれを返す。"""
    import json
    from datetime import date

    today_str = date.today().strftime("%Y-%m-%d")
    out_dir   = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / f"{today_str}_aio.json"
    ss_dir     = out_dir / today_str
    ss_dir.mkdir(exist_ok=True)

    if cache_path.exists():
        print(f"  AIOキャッシュ使用: {cache_path}")
        return json.loads(cache_path.read_text(encoding="utf-8"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright 未インストール。pip install playwright && playwright install chromium", file=sys.stderr)
        return []

    _TCD_KEYS = ["tcd.jp", "株式会社tcd", "tcd co", "（tcd）", "TCD"]
    results: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        for query in queries:
            print(f"  AIO確認中: 「{query}」...")
            encoded = query.replace(" ", "+")
            url = f"https://www.google.co.jp/search?q={encoded}&hl=ja&gl=jp"
            aio_exists   = False
            aio_excerpt  = None
            tcd_mentioned = False
            cited_urls   : list[str] = []
            cited_companies: list[str] = []
            error        = None

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(wait_ms)

                html = page.content()

                # AIO有無判定（日本語見出しテキストで検出）
                aio_exists = "AI による概要" in html or "AIによる概要" in html

                if aio_exists:
                    # AIOブロックのテキストを抽出
                    for sel in [
                        "div[data-attrid='SGE']",
                        "[jsname='Cpkphb']",
                        "div.LLtSOc",
                        "div.wDYxhc",
                    ]:
                        el = page.query_selector(sel)
                        if el:
                            aio_excerpt = el.inner_text()[:400].strip()
                            break
                    # テキスト検索フォールバック
                    if not aio_excerpt:
                        try:
                            el = page.locator("text=AI による概要").first
                            parent = el.locator("xpath=../..").first
                            aio_excerpt = parent.inner_text()[:400].strip()
                        except Exception:
                            pass

                    # TCD言及
                    check_text = (aio_excerpt or "") + html[:8000]
                    tcd_mentioned = any(k.lower() in check_text.lower() for k in _TCD_KEYS)

                    # 引用URL（AIOブロック内のリンク）
                    try:
                        links = page.locator("a[href*='http']").all()
                        for lnk in links[:30]:
                            href = lnk.get_attribute("href") or ""
                            if href.startswith("http") and "google" not in href:
                                cited_urls.append(href)
                        cited_urls = list(dict.fromkeys(cited_urls))[:5]
                    except Exception:
                        pass

                # スクリーンショット
                ss_name = query.replace(" ", "_") + ".png"
                ss_path = str(ss_dir / ss_name)
                page.screenshot(path=ss_path, clip={"x": 0, "y": 0, "width": 900, "height": 700})

            except Exception as e:
                error = str(e)
                ss_path = None

            results.append({
                "query":            query,
                "observed_at":      today_str,
                "aio_exists":       aio_exists,
                "tcd_mentioned":    tcd_mentioned,
                "aio_excerpt":      aio_excerpt,
                "cited_companies":  cited_companies,
                "cited_urls":       cited_urls,
                "screenshot_path":  ss_path if not error else None,
                "error":            error,
            })

        browser.close()

    cache_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  AIO観測結果保存: {cache_path}")
    return results


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
    if not gaio:
        md.append("観測データなし（playwright 未インストールまたは無効）。\n")
    else:
        md += [
            "| クエリ | AIO有無 | TCD言及 | 引用URL | エラー |",
            "|--------|--------|--------|--------|--------|",
        ]
        for r in gaio:
            aio  = "✅" if r.get("aio_exists") else ("⬜" if r.get("aio_exists") is False else "?")
            tcd  = "✅" if r.get("tcd_mentioned") else "なし"
            urls = " ".join(r.get("cited_urls", [])[:2]) or "-"
            err  = r.get("error", "") or ""
            md.append(f"| {r['query']} | {aio} | {tcd} | {urls} | {err[:40]} |")
        md.append("")
        for r in gaio:
            if r.get("aio_excerpt"):
                md.append(f"**{r['query']}** — 抜粋: {r['aio_excerpt'][:200]}")
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

    def finding_row(text: str) -> str:
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="background:{INFO_BG};border-left:4px solid {CYAN};border-radius:0 5px 5px 0;'
            f'padding:11px 16px;color:{TEXT};font-size:13px;line-height:1.6;">&#128269; {esc(text)}</div></td></tr>'
        )

    def hypothesis_row(text: str) -> str:
        return (
            f'<tr><td style="padding-bottom:8px;">'
            f'<div style="background:{HYPO_BG};border-left:4px solid #cbd5e1;border-radius:0 5px 5px 0;'
            f'padding:11px 16px;color:{TEXT};font-size:13px;line-height:1.6;">&#128270; {esc(text)}</div></td></tr>'
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
            found.append("データ蓄積中。次月以降の比較対象としてベースラインを記録した。")
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

    findings_html    = "".join(finding_row(f)    for f in gen_findings())
    hypothesis_html  = "".join(hypothesis_row(h) for h in gen_hypothesis())
    checkpoints_html = "".join(check_row(c)      for c in gen_html_checkpoints())

    # ── Google AI Overview セクション ──
    def _google_aio_section() -> str:
        gaio = data.get("google_aio", [])
        title_html = sec_title("3", "Google AI Overview 観測")
        note_row = (
            f'<tr><td style="padding-bottom:8px;">'
            f'<p style="color:{MUTED};font-size:11px;margin:0;">'
            f'Google 検索結果上の AI Overview（AIによる概要）の有無とTCD言及を観測しています。'
            f'</p></td></tr>'
        )
        if not gaio:
            return (
                title_html + note_row +
                f'<tr><td style="padding-bottom:16px;">'
                f'<p style="color:{MUTED};font-size:12px;margin:0;">'
                f'観測データなし（playwright 未インストールまたは無効）。</p></td></tr>'
            )
        hdr = (
            th("クエリ", "left", "160") +
            th("AIO有無", "center", "70") +
            th("TCD言及", "center", "70") +
            th("引用URL（上位）")
        )
        rows_html = ""
        for r in gaio:
            aio_color = UP if r.get("aio_exists") else MUTED
            aio_txt   = "&#9989; あり" if r.get("aio_exists") else "⬜ なし"
            if r.get("aio_exists") is None:
                aio_txt = f'<span style="color:{MUTED};">?</span>'
            tcd_color = UP if r.get("tcd_mentioned") else MUTED
            tcd_txt   = "&#9989; 言及あり" if r.get("tcd_mentioned") else "なし"
            urls = r.get("cited_urls", [])
            url_html  = "<br>".join(
                f'<a href="{esc(u)}" style="color:{CYAN};font-size:11px;text-decoration:none;">{esc(u[:55])}…</a>'
                for u in urls[:2]
            ) or f'<span style="color:{MUTED};font-size:11px;">-</span>'
            if r.get("error"):
                rows_html += (
                    f'<tr><td colspan="4" style="padding:7px 10px;font-size:12px;color:{DOWN};">'
                    f'⚠ {esc(r["query"])}: {esc(r["error"][:80])}</td></tr>\n'
                )
            else:
                rows_html += (
                    "<tr>"
                    + td_cell(esc(r["query"]))
                    + td_cell(f'<span style="color:{aio_color};font-weight:700;">{aio_txt}</span>', "center")
                    + td_cell(f'<span style="color:{tcd_color};font-weight:700;">{tcd_txt}</span>', "center")
                    + td_cell(url_html)
                    + "</tr>\n"
                )
                if r.get("tcd_mentioned") and r.get("aio_excerpt"):
                    rows_html += (
                        f'<tr style="background:#f8fafc;"><td colspan="4" '
                        f'style="padding:5px 10px 7px;font-size:11px;color:{MUTED};">'
                        f'&#128203; 抜粋: {esc(r["aio_excerpt"][:120])}…</td></tr>\n'
                    )
        table_html = wrap_table(hdr, rows_html)
        return title_html + note_row + f'<tr><td style="padding-bottom:16px;">{table_html}</td></tr>'

    google_aio_html = _google_aio_section()

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

<!-- 2. 今週/月の発見 -->
{sec_title("2", f"今{period_unit}の発見")}
{findings_html}

<!-- 3. Google AI Overview 観測 -->
{google_aio_html}

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

<!-- 9. 今週/月の仮説 -->
{sec_title("9", f"今{period_unit}の仮説")}
{hypothesis_html}

<!-- 10. 次週/月確認ポイント -->
{sec_title("10", f"次{period_unit}確認ポイント")}
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
    if aio_cfg.get("enabled", False):
        print("Google AI Overview を観測中...")
        google_aio_data = fetch_google_aio(
            queries   = aio_cfg.get("queries", []),
            headless  = aio_cfg.get("headless", True),
            wait_ms   = aio_cfg.get("wait_ms", 2500),
            output_dir= str(Path(args.output_dir) / "aio_visibility"),
        )
    else:
        google_aio_data = []


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
