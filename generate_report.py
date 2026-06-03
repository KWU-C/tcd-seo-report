#!/usr/bin/env python3
"""
TCD SEO/AIO Monthly Report Generator
Google Search Console API を使用して TCD サイトの月次 SEO/AIO レポートを自動生成します

使い方:
  python generate_report.py
  python generate_report.py --credentials /path/to/service-account.json
  python generate_report.py --config config/aio_monitor.yml --output-dir reports
"""
from __future__ import annotations

import os
import sys
import argparse
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
import pandas as pd
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ────────────────────────────────────────────────────────────────────
# 定数
# ────────────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
DATA_LAG_DAYS = 3       # Search Console のデータ反映遅延（日）
PERIOD_DAYS = 28
SC_MAX_ROWS = 25_000    # Search Console API の1リクエスト上限

# 掲載順位別の期待CTR（Backlinko 2023 調査ベース）
EXPECTED_CTR_BY_POS: dict[int, float] = {
    1: 0.284, 2: 0.152, 3: 0.107, 4: 0.079, 5: 0.060,
    6: 0.047, 7: 0.038, 8: 0.031, 9: 0.026, 10: 0.022,
}

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic UI", sans-serif;
    max-width: 1200px; margin: 0 auto; padding: 24px 20px;
    color: #1a1a1a; line-height: 1.7; background: #fafafa;
  }}
  h1 {{ color: #0052cc; border-bottom: 3px solid #0052cc; padding-bottom: 10px; margin-top: 0; }}
  h2 {{
    color: #0052cc; border-bottom: 1px solid #cce0ff;
    padding-bottom: 6px; margin-top: 48px; font-size: 1.2rem;
  }}
  h2::before {{ content: "◆ "; color: #0052cc; }}
  h3 {{ color: #333; font-size: 1rem; margin-top: 24px; }}
  p {{ margin: 8px 0; }}
  strong {{ color: #0052cc; }}
  table {{
    width: 100%; border-collapse: collapse; margin: 12px 0;
    font-size: 13px; background: white;
    box-shadow: 0 1px 3px rgba(0,0,0,.08); border-radius: 6px; overflow: hidden;
  }}
  th {{
    background: #0052cc; color: white;
    padding: 10px 12px; text-align: left; white-space: nowrap;
  }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #edf2f7; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:nth-child(even) {{ background: #f7f9fc; }}
  tr:hover {{ background: #eaf2ff; }}
  code {{
    background: #f0f4ff; padding: 2px 6px;
    border-radius: 4px; font-size: 12px; color: #3730a3;
  }}
  blockquote {{
    border-left: 4px solid #0052cc; margin: 12px 0;
    padding: 10px 16px; background: #f0f7ff;
    color: #555; font-size: 13px; border-radius: 0 4px 4px 0;
  }}
  hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 32px 0; }}
  a {{ color: #0052cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  li {{ margin: 4px 0; }}
  @media print {{
    body {{ max-width: 100%; background: white; }}
    table {{ box-shadow: none; }}
  }}
</style>
</head>
<body>
{body}
</body>
</html>"""


# ────────────────────────────────────────────────────────────────────
# 設定ファイル読み込み
# ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config/aio_monitor.yml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ────────────────────────────────────────────────────────────────────
# 認証
# ────────────────────────────────────────────────────────────────────

def get_service(credentials_path: str | None = None) -> Any:
    """認証済み Search Console API サービスを返す。
    優先順: 引数 → 環境変数 GOOGLE_APPLICATION_CREDENTIALS → OAuth2 フロー"""

    # サービスアカウント（自動化推奨）
    sa_path = credentials_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if sa_path and Path(sa_path).exists():
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=SCOPES
        )
        return build("searchconsole", "v1", credentials=creds, cache_discovery=False)

    # OAuth2（ブラウザ認証、トークンをキャッシュ）
    creds = None
    token_path = Path("token.pickle")
    if token_path.exists():
        creds = pickle.loads(token_path.read_bytes())

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            oauth_file = Path("credentials.json")
            if not oauth_file.exists():
                print(
                    "\nエラー: 認証情報が見つかりません。以下いずれかを設定してください:\n"
                    "  【サービスアカウント】\n"
                    "    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json\n"
                    "  【OAuth2】\n"
                    "    Google Cloud Console から OAuth2 credentials.json をダウンロードし\n"
                    "    プロジェクトルートに配置",
                    file=sys.stderr,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(oauth_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_bytes(pickle.dumps(creds))

    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


# ────────────────────────────────────────────────────────────────────
# 期間計算
# ────────────────────────────────────────────────────────────────────

def get_periods() -> tuple[str, str, str, str]:
    """(cur_start, cur_end, prv_start, prv_end) を YYYY-MM-DD 形式で返す。
    DATA_LAG_DAYS 分だけ過去にずらしてデータ完全性を確保。"""
    today = datetime.today()
    end = today - timedelta(days=DATA_LAG_DAYS)
    start = end - timedelta(days=PERIOD_DAYS - 1)
    prv_end = start - timedelta(days=1)
    prv_start = prv_end - timedelta(days=PERIOD_DAYS - 1)
    fmt = lambda d: d.strftime("%Y-%m-%d")
    return fmt(start), fmt(end), fmt(prv_start), fmt(prv_end)


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


def kw_compare(cdf: pd.DataFrame, pdf: pd.DataFrame, keywords: list[str]) -> list[dict]:
    out = []
    for kw in keywords:
        cr = cdf[cdf["query"].str.lower() == kw.lower()]
        pr = pdf[pdf["query"].str.lower() == kw.lower()]
        cd = cr.iloc[0].to_dict() if not cr.empty else {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        pd_ = pr.iloc[0].to_dict() if not pr.empty else {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        out.append({
            "query": kw,
            "cur": cd, "prv": pd_,
            "click_chg": pct_chg(float(cd["clicks"]), float(pd_["clicks"])),
            "pos_chg": float(cd["position"]) - float(pd_["position"]),
            "ctr_chg": float(cd["ctr"]) - float(pd_["ctr"]),
        })
    return out


def page_compare(cdf: pd.DataFrame, pdf: pd.DataFrame, cfgs: list[dict], site_url: str) -> list[dict]:
    base = site_url.rstrip("/")
    out = []
    for cfg in cfgs:
        path = cfg.get("url", "")
        name = cfg.get("name", path)
        full_url = (base + path) if path.startswith("/") else path

        cr = cdf[cdf["page"] == full_url]
        pr = pdf[pdf["page"] == full_url]
        cd = cr.iloc[0].to_dict() if not cr.empty else {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        pd_ = pr.iloc[0].to_dict() if not pr.empty else {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        score = _aio_score(float(cd["clicks"]), float(cd["impressions"]), float(cd["position"]))

        out.append({
            "name": name, "url": full_url,
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

def gen_insights(data: dict) -> str:
    tc = data["totals"]["current"]
    tp = data["totals"]["previous"]
    lines: list[str] = []

    # 全体クリック傾向
    cc = pct_chg(tc["clicks"], tp["clicks"])
    if cc is not None:
        if cc > 10:
            lines.append(f"- ✅ 総クリック数が前期比 **{cc:.1f}% 増加**。全体トレンドは良好です。")
        elif cc < -10:
            lines.append(f"- ⚠️ 総クリック数が前期比 **{abs(cc):.1f}% 減少**。要因分析を推奨します。")
        else:
            lines.append(f"- → 総クリック数は概ね横ばい（{cc:+.1f}%）。")

    # 平均順位
    pos_d = tc["position"] - tp["position"]
    if pos_d < -0.5:
        lines.append(f"- ✅ 平均掲載順位が **{abs(pos_d):.1f} 位改善**（{tp['position']:.1f} → {tc['position']:.1f}）。")
    elif pos_d > 0.5:
        lines.append(f"- ⚠️ 平均掲載順位が **{pos_d:.1f} 位低下**（{tp['position']:.1f} → {tc['position']:.1f}）。")

    # CTR 変化
    ctr_d = tc["ctr"] - tp["ctr"]
    if abs(ctr_d) > 0.005:
        if ctr_d < 0:
            lines.append(f"- ⚠️ CTR が **{abs(ctr_d) * 100:.2f}pp 低下**。AI Overview の影響・SERP 変化を確認してください。")
        else:
            lines.append(f"- ✅ CTR が **{ctr_d * 100:.2f}pp 改善**。")

    # AIO 異常クエリ
    high_aio = [a for a in data["aio_anomalies"] if a["aio"] > 0.6]
    if high_aio:
        qs = "、".join(f'「{a["query"]}」' for a in high_aio[:3])
        lines.append(
            f"- 🔴 **AIOシグナル高**: {qs} でCTR損失が疑われます。"
            "E-E-A-T 強化・コンテンツの一次情報化を検討してください。"
        )

    # 重点KW 順位下落
    for r in data.get("key_kws", []):
        if r["pos_chg"] > 3:
            lines.append(
                f"- ⚠️ 重点KW「**{r['query']}**」が {r['pos_chg']:.1f} 位低下。"
                "コンテンツ・被リンク状況の確認を推奨。"
            )

    # LP/CV ページのクリック急減
    for r in data.get("lp_pages", []):
        if r["click_chg"] is not None and r["click_chg"] < -20:
            lines.append(
                f"- ⚠️ CV ページ「**{r['name']}**」のクリックが {r['click_chg']:.1f}% 減少。"
                "SERP 表示・LPの内容を確認してください。"
            )

    if not lines:
        lines.append("- 特に注目すべき異常は検出されませんでした。引き続き継続監視を。")

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
        lines.append(
            f"| {r['query']} "
            f"| {fi(cd['clicks'])} | {fchg(r['click_chg'])} "
            f"| {fi(cd['impressions'])} | {fp(cd['ctr'])} "
            f"| {fpos(cd['position'])} | {arr}{fpos_chg(r['pos_chg'])} |"
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

    md: list[str] = []

    # ── ヘッダー ──
    md += [
        f"# TCD.jp SEO/AIO 月次レポート",
        "",
        f"| | 期間 |",
        f"|---|---|",
        f"| **今期** | {cs} 〜 {ce}（{PERIOD_DAYS}日間） |",
        f"| **前期** | {ps} 〜 {pe}（{PERIOD_DAYS}日間） |",
        f"| **生成日時** | {datetime.now().strftime('%Y年%m月%d日 %H:%M')} |",
        "",
        "---",
        "",
    ]

    # ── 1. サイト全体サマリー ──
    md += ["## 1. サイト全体サマリー", ""]
    md += [
        "| 指標 | 今期 | 前期 | 増減 | 増減率 |",
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

    # ── 3. AIO 重点ページ ──
    md += [
        "## 3. AIO重点ページ分析",
        "",
        "> **AIOスコア**: 掲載順位に対してCTRが期待値を下回る度合い。高いほどAI Overviewによるクリック損失の可能性が高い。",
        "",
    ]
    md.append(_page_table_md(data["aio_pages"], site_url))

    # ── 4. Definition 記事 ──
    md += ["## 4. Definition記事分析", ""]
    md.append(_page_table_md(data["def_pages"], site_url))

    # ── 5. LP/CV ページ ──
    md += ["## 5. LP/CVページ分析", ""]
    md.append(_page_table_md(data["lp_pages"], site_url))

    # ── 6. デバイス別 ──
    md += ["## 6. デバイス別分析", ""]
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

    # ── 7. AIO シグナル検出 ──
    md += [
        "## 7. AIOシグナル検出（CTR損失疑いクエリ）",
        "",
        "> 掲載順位に対してCTRが著しく低いクエリ一覧。AI Overview に回答を奪われている可能性があります。",
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

    # ── 8. 上位クエリ ──
    md += [f"## 8. 上位クエリ TOP{top_n}", ""]
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

    # ── 9. 上位ページ ──
    md += [f"## 9. 上位ページ TOP{top_n}", ""]
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

    # ── 10. インサイト・推奨アクション ──
    md += ["## 10. 注目ポイント・改善推奨", ""]
    md.append(gen_insights(data))
    md.append("")

    return "\n".join(md)


# ────────────────────────────────────────────────────────────────────
# HTML レポート生成
# ────────────────────────────────────────────────────────────────────

def gen_html(md_content: str, title: str = "TCD SEO/AIO 月次レポート") -> str:
    try:
        import markdown as md_lib
        body = md_lib.markdown(
            md_content,
            extensions=["tables", "fenced_code", "nl2br"],
        )
    except ImportError:
        # markdown ライブラリが無い場合は pre タグで代替
        body = f"<pre>{md_content}</pre>"

    return HTML_TEMPLATE.format(title=title, body=body)


# ────────────────────────────────────────────────────────────────────
# メイン
# ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="TCD SEO/AIO 月次レポート生成スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config/aio_monitor.yml", help="設定ファイルパス")
    parser.add_argument("--credentials", help="サービスアカウント JSON パス")
    parser.add_argument("--output-dir", default="reports", help="レポート出力先ディレクトリ")
    parser.add_argument("--no-html", action="store_true", help="HTML 出力をスキップ")
    args = parser.parse_args()

    # 設定読み込み
    config = load_config(args.config)
    site_url: str = config["site_url"]
    top_n: int = config.get("report", {}).get("top_n", 20)
    aio_min_impr: int = config.get("report", {}).get("aio_min_impressions", 100)

    # 認証
    print("Search Console API に接続中...")
    service = get_service(args.credentials)

    # 期間確定
    cur_start, cur_end, prv_start, prv_end = get_periods()
    print(f"  今期: {cur_start} 〜 {cur_end}")
    print(f"  前期: {prv_start} 〜 {prv_end}")

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

    # 集計・分析
    print("データ集計中...")
    data = {
        "periods": ((cur_start, cur_end), (prv_start, prv_end)),
        "totals": {
            "current": calc_totals(cur_pages),
            "previous": calc_totals(prv_pages),
        },
        "key_kws": kw_compare(cur_queries, prv_queries, config.get("key_keywords", [])),
        "aio_pages": page_compare(cur_pages, prv_pages, config.get("aio_focus_pages", []), site_url),
        "def_pages": page_compare(cur_pages, prv_pages, config.get("definition_articles", []), site_url),
        "lp_pages": page_compare(cur_pages, prv_pages, config.get("lp_cv_pages", []), site_url),
        "devices": cur_devices.to_dict("records") if not cur_devices.empty else [],
        "aio_anomalies": detect_aio_anomalies(cur_queries, min_impr=aio_min_impr),
        "top_queries": top_queries_cmp(cur_queries, prv_queries, n=top_n),
        "top_pages": top_pages_cmp(cur_pages, prv_pages, n=top_n),
    }

    # レポート生成
    print("レポート生成中...")
    md_content = gen_markdown(data, config)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{datetime.now().strftime('%Y-%m')}-seo-aio-report"

    md_path = output_dir / f"{base_name}.md"
    md_path.write_text(md_content, encoding="utf-8")
    print(f"  Markdown: {md_path}")

    if not args.no_html:
        html_content = gen_html(md_content)
        html_path = output_dir / f"{base_name}.html"
        html_path.write_text(html_content, encoding="utf-8")
        print(f"  HTML:     {html_path}")

    print("\n完了!")


if __name__ == "__main__":
    main()
