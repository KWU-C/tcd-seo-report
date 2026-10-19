"""表示層の共通View Model。

analysis.json を主入力とする。表示用の閾値（順位変化の横ばい/軽微/大幅の3段階、
Impressions下限）のみ config_v2/thresholds.yaml から読む（新しい分析判定・
閾値判定は追加しない。Finding.severity/confidence/judgmentは既にanalysisレイヤーで
確定済みの値をそのまま使い、ここでは「どれを本文に出すか／どうグルーピングするか」
という表示上の取捨選択のみ行う）。

レポート構成（4セクション + Appendix。2026-10-13改善指示書で「全体アクセス → SEO順位 → AIO引用 → 詳細」
の流れに再構成。表示順の正本はrender_html.py/render_md.py）:
  ① サイト全体のアクセス状況（GA4） … 1段目: サイト全体/マガジン/Works/Service、2段目: 問い合わせ/資料請求
  ② 今週の要点                      … 最大3項目（アクセス → SEO順位 → AIO引用）。数値の羅列ではなく変化を書く
  ③ SEO検索順位                     … 重点4キーワードを1つの表で表示（順位・前週比・Imp・Click）
  ④ AIO引用状況                     … 同じ4キーワードのAIO生成／TCD引用／主な引用元（最大3件）を独立した表で表示
  Appendix                          … キーワード別詳細（クエリ内訳・AIO引用元全件・判定）・ページ別・未登録施策・データ品質等
重点商談市場（deal）／テーマ市場（theme）の区分は内部データ（tier）として残し、見出しには出さない。
参考クエリ（インナーブランディング会社×地域）はAppendixのキーワード別詳細に1行で表示する。

render_md.py / render_html.py / render_json.py は、この関数が返す同一の
View Modelだけを参照することで、出力形式間の内容差異を防ぐ。
"""
from __future__ import annotations

import re

from src_v2 import config_loader

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
_CONFIDENCE_RANK = {"High": 0, "Medium": 1, "Low": 2, "Not assessable": 3}
_CATEGORY_RANK = {"market": 0, "competitor": 1, "initiative": 2, "page": 3, "magazine_landing_page": 4}

_LOW_CONFIDENCE_LEVELS = {"Low", "Not assessable"}
_ACTIONABLE_JUDGMENTS = {"実施", "再調査"}

_CATEGORY_LABELS = {
    ("service", "service"): "サービス案内系",
    ("service", "definition"): "サービスDefinition系",
    ("branding", "definition"): "マガジンDefinition系",
}

_REGION_LABEL = {"tokyo": "東京", "osaka": "大阪"}
_MARKET_LABEL = {"branding": "ブランディング", "inner_branding": "インナーブランディング"}

# 「表示回数・クリック数が少なく参考値（impressions=.., clicks=..）」等、
# confidenceバッジ（Low/Not assessable）と意味が重複する定型文。
# 数値はFinding.evidenceやテーブル列に別途出すため、文中では繰り返さない。
_LOW_DATA_BOILERPLATE = re.compile(r"表示回数・クリック数が少なく参考値(?:（[^）]*）)?／?")


def _condensed_message(message: str) -> str:
    """定型文（少数データの参考値注記）は行ごとに繰り返さず、セクション先頭で1回だけ
    注記する（report.html改善指示書へのユーザー追加フィードバック②）。
    定型文のみのメッセージは空文字を返し、表示側で「-」にする。
    """
    return _LOW_DATA_BOILERPLATE.sub("", message).strip("／ ")


def _sort_key(finding: dict) -> tuple:
    """表示優先順位: severity → confidence → category。"""
    return (
        _SEVERITY_RANK.get(finding["severity"], 99),
        _CONFIDENCE_RANK.get(finding["confidence"], 99),
        _CATEGORY_RANK.get(finding["category"], 99),
    )


def sort_findings(findings: list[dict]) -> list[dict]:
    return sorted(findings, key=_sort_key)


def _display_flag(finding: dict) -> str | None:
    if finding["confidence"] in _LOW_CONFIDENCE_LEVELS or finding.get("judgment") == "判断保留":
        return "要確認・判断保留"
    return None


def judgment_label(finding: dict) -> str:
    """「判断｜不足：X」または「判断｜要調査：X」形式の表示ラベル。どちらもなければ判断のみ。"""
    judgment = finding.get("judgment") or "-"
    shortfall = finding.get("shortfall")
    if shortfall:
        return f"{judgment}｜不足：{shortfall}"
    investigate = finding.get("investigate")
    return f"{judgment}｜要調査：{investigate}" if investigate else judgment


def _annotate(finding: dict) -> dict:
    return {
        **finding,
        "judgment_label": judgment_label(finding),
        "display_flag": _display_flag(finding),
        "display_message": _condensed_message(finding["message"]),
    }


def _position_delta(evidence: dict) -> float | None:
    cur_pos = (evidence or {}).get("current", {}).get("position")
    prv_pos = (evidence or {}).get("comparison", {}).get("position")
    if cur_pos is None or prv_pos is None:
        return None
    return prv_pos - cur_pos  # 正 = 順位改善（数値が小さくなった）


def delta_tier(delta: float | None, display_cfg: dict) -> str:
    """順位変化幅の3段階表示区分。flat: 横ばい / minor: 軽微な変化 / major: 改善・悪化。"""
    if delta is None:
        return "flat"
    magnitude = abs(delta)
    if magnitude < display_cfg["position_delta_flat"]:
        return "flat"
    if magnitude < display_cfg["position_delta_minor"]:
        return "minor"
    return "major"


def _category_label(group: str, category: str) -> str:
    return _CATEGORY_LABELS.get((group, category), f"{group}/{category}")


_CV_GA4_SEGMENTS = ("contact", "document_request")

# 重点市場の2階層（2026-09-30）。referenceは独立カードにせず、同じmarketのテーマ市場カードに参考行として添える
_TIER_GROUPS = [("deal", "重点商談市場"), ("theme", "テーマ市場")]
_REGION_ORDER = {"tokyo": 0, "osaka": 1}
_MARKET_ORDER = {"branding": 0, "inner_branding": 1}


# ── ① サイトアクセス状況（GA4） ─────────────────────────────────────

def _build_site_usage(ga4_summary: dict) -> dict:
    segments = []
    for key, seg in (ga4_summary or {}).items():
        cur, prv = seg.get("current"), seg.get("comparison")
        delta_pct = None
        if cur is not None and prv:
            delta_pct = (cur - prv) / prv * 100
        segments.append({
            "key": key, "label": seg.get("label", key),
            "current": cur, "comparison": prv, "delta_pct": delta_pct,
            "error": seg.get("error"),
        })
    # 1段目: サイト全体・各セクション到達、2段目: CVに近いページ到達（問い合わせ・資料請求）
    top = [seg for seg in segments if seg["key"] not in _CV_GA4_SEGMENTS]
    cv = [seg for seg in segments if seg["key"] in _CV_GA4_SEGMENTS]
    return {"segments": segments, "rows": [row for row in (top, cv) if row]}


# ── ③ 重点市場（4市場固定・一覧表 + 詳細カード） ──────────────────

_SEO_STATUS_LABELS = [
    (10, "上位表示"),
    (20, "1ページ目目前"),
    (30, "20位圏"),
    (None, "30位以下"),
]


def _seo_status_label(position: float | None, impressions: int) -> str:
    if position is None or impressions == 0:
        return "表示なし"
    for threshold, label in _SEO_STATUS_LABELS:
        if threshold is None or position <= threshold:
            return label
    return "30位以下"


def _market_label(evidence: dict) -> str:
    """deal: 「ブランディング会社 東京」、theme: 「ブランディング」、reference: 「会社×東京」。"""
    market = _MARKET_LABEL.get(evidence.get("market"), evidence.get("market") or "")
    region = _REGION_LABEL.get(evidence.get("region"), evidence.get("region") or "")
    tier = evidence.get("tier")
    if tier == "theme":
        return market
    if tier == "reference":
        return f"会社×{region}"
    return f"{market}会社 {region}"


def _short_label(evidence: dict) -> str:
    """Executive Summary用。deal は地域名のみ（東京/大阪）、theme はテーマ名。"""
    if evidence.get("tier") == "deal":
        return _REGION_LABEL.get(evidence.get("region"), evidence.get("region") or "")
    return _market_label(evidence)


def _volume_label(evidence: dict) -> str | None:
    volume = evidence.get("search_volume_ref")
    return f"約{volume:,}" if volume else None


_AIO_TOP_SOURCES = 3


def _aio_view(evidence: dict) -> dict:
    """④ AIO引用状況の1行分。AIO生成／TCD引用／引用元を別々の列にする。
    未取得（観測なし・失敗）は「生成なし」と、引用元取得不可は「引用元なし」「TCD引用なし」と混同しない。"""
    aio = evidence.get("aio") or {}
    citations = evidence.get("aio_citations") or {}
    domains = citations.get("domains")
    if domains is None:  # aio_citations導入前のanalysis.json
        domains = list(dict.fromkeys(d for d in (aio.get("cited_domains") or []) if d and d != "tcd.jp"))

    unknown_count = citations.get("unknown_count") or 0
    view = {"aio_generated_label": "未取得", "tcd_cited_label": "—", "aio_sources_label": "—",
            "aio_domains": domains, "aio_unknown_source_count": unknown_count,
            "tcd_cited": None, "citation_unavailable": False}
    if aio.get("acquisition_status") not in ("success", "not_generated"):
        return view
    if not aio.get("aio_generated"):
        return {**view, "aio_generated_label": "なし"}
    if citations.get("status") == "unavailable":
        # AIO枠はあるが本文を取得できていない。生成「あり」とは断定しない
        return {**view, "aio_generated_label": "未確認", "tcd_cited_label": "判定不可",
                "aio_sources_label": "取得不可（AIO本文・引用元を取得できず）", "citation_unavailable": True}

    tcd_cited = bool(aio.get("tcd_cited"))
    sources = "、".join(domains[:_AIO_TOP_SOURCES]) + (" など" if len(domains) > _AIO_TOP_SOURCES else "")
    # 引用元はあるがドメインを特定できなかった（Google経由リンク等）場合は「引用元なし」と表示しない
    empty_label = f"引用元不明（{unknown_count}件）" if unknown_count else "引用元なし"
    return {**view, "aio_generated_label": "あり", "tcd_cited_label": "あり" if tcd_cited else "なし",
            "aio_sources_label": sources or empty_label, "tcd_cited": tcd_cited}


def _build_priority_markets(market_findings: list[dict], competitor_by_query: dict[str, list[dict]], thresholds_cfg: dict) -> dict:
    display_cfg = thresholds_cfg["display"]
    low_impressions = thresholds_cfg["low_data"]["low_impressions"]
    rows_by_tier: dict[str, list[tuple[dict, dict]]] = {}
    for f in market_findings:
        evidence = f.get("evidence") or {}
        qid = (f.get("source") or {}).get("query_id")
        cur = evidence.get("current") or {}
        position = cur.get("position")
        impressions = cur.get("impressions") or 0
        clicks = cur.get("clicks") or 0
        delta = _position_delta(evidence)
        min_impressions = evidence.get("min_impressions") or low_impressions
        # 表示回数が市場ごとの最低Imp未満の場合は順位差を強調しない（一律50impは使わない）
        tier = delta_tier(delta, display_cfg) if impressions >= min_impressions else "flat"

        competitor_summary = evidence.get("competitor_summary") or {}
        aio_view = _aio_view(evidence)
        row = {
            "tier": evidence.get("tier", "deal"),
            "market": evidence.get("market"),
            "market_label": _market_label(evidence),
            "short_label": _short_label(evidence),
            "volume_label": _volume_label(evidence),
            "query": evidence.get("query"),
            "seo_status": _seo_status_label(position, impressions),
            "position": position,
            "prev_position": (evidence.get("comparison") or {}).get("position"),
            "position_delta": delta,
            "delta_tier": tier,
            "impressions": impressions,
            "clicks": clicks,
            "min_impressions": min_impressions,
            # AND一致（語順不問）で集計するキーワードは、関連クエリを含む集計であることを表示する
            "includes_related_queries": bool(evidence.get("match_tokens")),
            **aio_view,
            "competitor_comparison_available": competitor_summary.get("comparison_available", False),
            "judgment": f.get("judgment"),
            "judgment_label": judgment_label(f),
        }
        card = {
            **f,
            "card_title": row["market_label"],
            "volume_label": row["volume_label"],
            "competitor_findings": competitor_by_query.get(qid, []),
            "position_delta": delta,
            "delta_tier": tier,
        }
        row["seo_movement"] = _seo_movement(row, thresholds_cfg)
        row["delta_label"] = _delta_label(row)
        card["aio_view"] = aio_view
        rows_by_tier.setdefault(row["tier"], []).append((row, card))

    def _order(item: tuple[dict, dict]) -> tuple:
        ev = item[1].get("evidence") or {}
        return (_REGION_ORDER.get(ev.get("region"), 99), _MARKET_ORDER.get(ev.get("market"), 99))

    references = sorted(rows_by_tier.get("reference", []), key=_order)
    groups = []
    for tier_key, label in _TIER_GROUPS:
        items = sorted(rows_by_tier.get(tier_key, []), key=_order)
        cards = []
        for row, card in items:
            if tier_key == "theme":
                card["reference_rows"] = [r for r, _ in references if r["market"] == row["market"]]
            cards.append(card)
        groups.append({"key": tier_key, "label": label, "table": [r for r, _ in items], "cards": cards})
    return {"groups": groups, "table": [r for g in groups for r in g["table"]]}


# ── ④ カテゴリ・ページ動向 ────────────────────────────────────────

def _build_category_summary(page_findings: list[dict]) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for f in page_findings:
        evidence = f.get("evidence") or {}
        key = (evidence.get("group"), evidence.get("category"))
        buckets.setdefault(key, []).append(f)

    summary = []
    for (group, category), items in buckets.items():
        cur_impressions = sum((i.get("evidence", {}).get("current", {}).get("impressions") or 0) for i in items)
        cur_clicks = sum((i.get("evidence", {}).get("current", {}).get("clicks") or 0) for i in items)
        prv_impressions = sum((i.get("evidence", {}).get("comparison", {}).get("impressions") or 0) for i in items)
        prv_clicks = sum((i.get("evidence", {}).get("comparison", {}).get("clicks") or 0) for i in items)

        judgment_counts: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        for i in items:
            j = i.get("judgment")
            if j:
                judgment_counts[j] = judgment_counts.get(j, 0) + 1
            severity_counts[i["severity"]] = severity_counts.get(i["severity"], 0) + 1

        summary.append({
            "group": group, "category": category, "label": _category_label(group, category),
            "page_count": len(items),
            "current": {"impressions": cur_impressions, "clicks": cur_clicks},
            "comparison": {"impressions": prv_impressions, "clicks": prv_clicks},
            "judgment_counts": judgment_counts,
            "severity_counts": severity_counts,
        })

    return sorted(summary, key=lambda s: s["label"])


def _is_notable_page(f: dict, thresholds_cfg: dict, priority_market_urls: set[str]) -> bool:
    """本文（④）に掲載するページの条件（report.html改善指示書 3節③・5節）。
    Impressions>=50 かつ 順位差>=3、またはクリック差>=2、または重点市場の主対象URL、
    またはコンバージョン導線ページ（Low Confidenceでも常に本文表示）。
    それ以外はAppendix行き（少数Impressionsの順位変化で本文を占有させない）。
    """
    evidence = f.get("evidence") or {}
    url = evidence.get("url")
    if url in priority_market_urls:
        return True
    if "conversion" in (evidence.get("objective") or []):
        return True

    low_impressions = thresholds_cfg["low_data"]["low_impressions"]
    cur = evidence.get("current") or {}
    prv = evidence.get("comparison") or {}
    impressions = cur.get("impressions") or 0
    delta = _position_delta(evidence)

    if impressions >= low_impressions and delta is not None and abs(delta) >= 3:
        return True

    click_delta = abs((cur.get("clicks") or 0) - (prv.get("clicks") or 0))
    if click_delta >= 2:
        return True

    return False


def _build_top_movers(notable_findings: list[dict], n: int = 5) -> tuple[list[dict], list[dict]]:
    with_delta = []
    for f in notable_findings:
        delta = _position_delta(f.get("evidence") or {})
        if delta is not None:
            with_delta.append({**f, "position_delta": delta})

    improved = sorted([f for f in with_delta if f["position_delta"] > 0], key=lambda f: -f["position_delta"])[:n]
    declined = sorted([f for f in with_delta if f["position_delta"] < 0], key=lambda f: f["position_delta"])[:n]
    return improved, declined


def _build_category_page_trends(page_findings: list[dict], thresholds_cfg: dict, priority_market_urls: set[str]) -> tuple[dict, list[dict]]:
    category_summary = _build_category_summary(page_findings)
    notable = [f for f in page_findings if _is_notable_page(f, thresholds_cfg, priority_market_urls)]
    other_pages = sort_findings([f for f in page_findings if f not in notable])
    improved, declined = _build_top_movers(notable, n=5)
    return {
        "category_summary": category_summary,
        "notable": {"improved": improved, "declined": declined},
    }, other_pages


# ── ⑤ 施策評価 ──────────────────────────────────────────────────

def _build_initiatives_section(initiative_findings: list[dict]) -> tuple[dict, list[dict]]:
    known = [f for f in initiative_findings if "implemented_at" in (f.get("evidence") or {})]
    unknown = [f for f in initiative_findings if f not in known]
    return {"known": sort_findings(known), "unknown_count": len(unknown)}, unknown


# ── ③ SEO検索順位の前週比表示 ─────────────────────────────────────

RELATED_QUERIES_NOTE = "関連クエリを含む集計"
_AIO_NOTE = "定点観測した検索結果（週1回・各キーワード1回）についての判定。Google検索全体で引用されていないという意味ではない。引用元の全件はAppendix。"


def _volume_as_of_label(as_of: str | None) -> str:
    """"2026-07" → "2026年7月"。"""
    if not as_of or "-" not in as_of:
        return as_of or "-"
    year, month = as_of.split("-")[:2]
    return f"{year}年{int(month)}月"


def _seo_notes(volume_as_of: str | None) -> list[str]:
    return [
        f"「{RELATED_QUERIES_NOTE}」のキーワードは、指定した語をすべて含む検索クエリ（語順不問）の合算。内訳はAppendix。",
        "Impが少ないキーワードの順位・前週比は参考値（改善・悪化の判定はしない）。",
        f"月間検索Vol.は{_volume_as_of_label(volume_as_of)}時点の参考値。",
    ]


def _seo_movement(row: dict, thresholds_cfg: dict) -> str:
    """キーワードのSEO状態を 改善/悪化/横ばい/参考値 に分類する。
    順位差がdisplay.position_delta_flat未満は横ばい。Impressionsがキーワードごとの最低Imp
    （検索Vol.考慮。一律50impではない）未満は参考値（少数データの順位差を改善・悪化と呼ばない）。"""
    if row["position"] is None or row["impressions"] < row["min_impressions"]:
        return "参考値"
    delta = row["position_delta"]
    if delta is None or abs(delta) < thresholds_cfg["display"]["position_delta_flat"]:
        return "横ばい"
    return "改善" if delta > 0 else "悪化"


def _signed(value: float) -> str:
    return f"{value:+.1f}".replace("-", "−")


def _delta_label(row: dict) -> str:
    """「+1.3 改善」「−1.7（参考値）」「+0.8（横ばい）」。表示なし・前週データなしは「—」。"""
    delta = row["position_delta"]
    if row["position"] is None or not row["impressions"] or delta is None:
        return "—"
    movement = row["seo_movement"]
    if movement in ("改善", "悪化"):
        return f"{_signed(delta)} {movement}"
    return f"{_signed(delta)}（{movement}）"


# ── ② 今週の要点 ────────────────────────────────────────────────

def _access_highlight(site_usage: dict, thresholds_cfg: dict) -> str | None:
    change_pct = thresholds_cfg["display"]["access_change_pct"]
    segments = [s for s in site_usage.get("segments") or [] if not s.get("error") and s.get("current") is not None]
    site = next((s for s in segments if s["key"] == "site_wide"), None)
    if site is None:
        return None
    delta = site.get("delta_pct")
    line = f"サイト全体は{site['current']:,}" + (f"（前週比{_signed(delta)}%）" if delta is not None else "") + "。"

    others = [s for s in segments if s["key"] != "site_wide" and s.get("delta_pct") is not None]
    up = [s["label"].removesuffix("到達") for s in others if s["delta_pct"] >= change_pct]
    down = [s["label"].removesuffix("到達") for s in others if s["delta_pct"] <= -change_pct]
    changes = []
    if up:
        changes.append(f"{'・'.join(up)}への到達は増加")
    if down:
        changes.append(f"{'・'.join(down)}への到達は減少")
    return line + ("、".join(changes) if changes else "主要ページへの到達は横ばい") + "。"


def _seo_highlight(rows: list[dict]) -> str | None:
    if not rows:
        return None
    moved, steady, hidden = [], [], []
    for r in rows:
        if r["position"] is None or not r["impressions"]:
            hidden.append(r["short_label"])
        elif r["seo_movement"] in ("改善", "悪化"):
            moved.append(f"{r['short_label']}{r['position']:.1f}位に{r['seo_movement']}（前週{r['prev_position']:.1f}位）")
        else:
            steady.append(f"{r['short_label']}{r['position']:.1f}位（{r['seo_movement']}）")
    sentences = ["、".join(moved) if moved else "重点キーワードに大きな順位変化なし"]
    if steady:
        sentences.append("、".join(steady))
    if hidden:
        sentences.append(f"{'・'.join(f'「{h}」' for h in hidden)}はGSCで表示データなし")
    return "SEOは" + "。".join(sentences) + "。"


def _aio_highlight(rows: list[dict]) -> str | None:
    if not rows:
        return None
    cited = [r["short_label"] for r in rows if r["tcd_cited"]]
    unavailable = [r["short_label"] for r in rows if r["citation_unavailable"]]
    unavailable_note = f"{'・'.join(unavailable)}はAIO本文を取得できず判定不可" if unavailable else ""
    if cited:
        return f"AIOは{'・'.join(cited)}でTCD引用あり" + (f"（{unavailable_note}）" if unavailable_note else "") + "。"
    return f"定点観測{len(rows)}クエリではTCDのAIO引用を確認できず" + (f"（うち{unavailable_note}）" if unavailable_note else "") + "。"


def _build_highlights(site_usage: dict, priority_markets: dict, thresholds_cfg: dict) -> list[str]:
    """今週の要点（最大3項目）。優先順位は アクセス → SEO順位 → AIO引用（2026-10-13改善指示書）。
    analysis.jsonの数値から機械的に組み立て、原因の推測・提案は書かない。"""
    rows = priority_markets["table"]
    lines = [_access_highlight(site_usage, thresholds_cfg), _seo_highlight(rows), _aio_highlight(rows)]
    return [line for line in lines if line]


def _period_notes(notes_cfg: list[dict], section: str, cur_end: str) -> list[str]:
    """config_v2/report_notes.yaml のうち、この対象期間に表示する注記（ISO日付の文字列比較）。"""
    return [
        n["text"] for n in notes_cfg
        if n.get("section") == section and n.get("after_cur_end", "") < cur_end <= n.get("until_cur_end", "9999-12-31")
    ]


def _data_quality_label(status: str, rows: list[dict]) -> str:
    """ヘッダーの「データ品質」。AIO本文を取得できなかったキーワードがある場合は、
    pass（または warning）だけで終わらせず、判定不可の件数と対象を明示する。"""
    unavailable = [r["short_label"] for r in rows if r["citation_unavailable"]]
    if not unavailable:
        return status
    return f"{status}（AIO {len(rows)}件中{len(unavailable)}件は本文取得不可：{'・'.join(unavailable)}）"


def build_view_model(analysis: dict, thresholds_cfg: dict | None = None, notes_cfg: list[dict] | None = None) -> dict:
    thresholds_cfg = thresholds_cfg or config_loader.load_thresholds()
    notes_cfg = config_loader.load_report_notes() if notes_cfg is None else notes_cfg
    display_cfg = thresholds_cfg["display"]

    findings = analysis["findings"]
    annotated = [_annotate(f) for f in findings]
    all_sorted = sort_findings(annotated)

    market_findings = sort_findings([f for f in annotated if f["category"] == "market"])
    competitor_findings = sort_findings([f for f in annotated if f["category"] == "competitor"])
    competitor_by_query: dict[str, list[dict]] = {}
    for f in competitor_findings:
        qid = (f.get("source") or {}).get("query_id")
        competitor_by_query.setdefault(qid, []).append(f)

    initiative_findings = sort_findings([f for f in annotated if f["category"] == "initiative"])
    page_findings = [f for f in annotated if f["category"] == "page"]
    landing_pages = [f for f in annotated if f["category"] == "magazine_landing_page"]

    priority_markets = _build_priority_markets(market_findings, competitor_by_query, thresholds_cfg)
    priority_market_urls = set()
    for f in market_findings:
        top_url = (f.get("evidence") or {}).get("top_url") or {}
        url = top_url.get("page_id_or_url")
        if url:
            priority_market_urls.add(url)

    # カテゴリ別動向・施策評価は本文から削除したが、Appendix（要確認以外のページ／未登録施策）には残す
    _, other_pages = _build_category_page_trends(page_findings, thresholds_cfg, priority_market_urls)
    _, unknown_initiatives = _build_initiatives_section(initiative_findings)

    site_usage = _build_site_usage(analysis.get("ga4_summary", {}))

    data_quality = analysis["data_quality"]
    data_summary = analysis.get("data_summary", {})

    header = {
        "period": analysis["period"],
        "comparison_available": data_summary.get("gsc", {}).get("comparison_available"),
        "data_quality_status": data_quality["status"],
        "data_quality_label": _data_quality_label(data_quality["status"], priority_markets["table"]),
        "weeks_available": analysis.get("weeks_available"),
        "low_impressions_threshold": thresholds_cfg["low_data"]["low_impressions"],
    }

    executive_summary = _build_highlights(site_usage, priority_markets, thresholds_cfg)
    volume_as_of = next(
        ((f.get("evidence") or {}).get("search_volume_as_of") for f in market_findings
         if (f.get("evidence") or {}).get("search_volume_as_of")), None)

    excluded_landing_pages = data_summary.get("magazine_landing_pages", {}).get("excluded_urls", [])

    confidence_breakdown = {
        level: sum(1 for f in annotated if f["confidence"] == level)
        for level in ("High", "Medium", "Low", "Not assessable")
    }

    appendix = {
        "all_findings": all_sorted,
        "other_pages": other_pages,
        "landing_pages": {"top": landing_pages, "excluded": excluded_landing_pages},
        "unknown_initiatives": unknown_initiatives,
        "data_quality": {
            **data_quality,
            "label": header["data_quality_label"],
            "data_summary": data_summary,
            "gsc_counts": data_summary.get("gsc", {}),
            "aio_status": data_summary.get("aio", {}),
            "finding_count": analysis["counts"]["total"],
            "severity_breakdown": analysis["counts"]["by_severity"],
            "confidence_breakdown": confidence_breakdown,
        },
    }

    return {
        "schema_version": analysis["schema_version"],
        "generated_at": analysis["generated_at"],
        "header": header,
        "executive_summary": {"blocks": executive_summary, "narrative": None},
        "site_usage": site_usage,
        "priority_markets": {
            **priority_markets,
            "search_volume_as_of": volume_as_of,
            "seo_notes": _seo_notes(volume_as_of),
            "aio_notes": _period_notes(notes_cfg, "aio", analysis["period"]["cur_end"]) + [_AIO_NOTE],
        },
        "appendix": appendix,
    }
