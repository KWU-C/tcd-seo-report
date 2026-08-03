"""View ModelからMarkdownを生成する。分析ロジックは追加しない（整形のみ）。

render_html.pyと同じView Model・同じセクション構成・同じ閾値フィルタを参照する
（Markdown/HTML間で内容差異が生じないようにするため）。
"""
from __future__ import annotations

import re

_REGION_LABEL = {"tokyo": "東京", "osaka": "大阪"}
_MARKET_LABEL = {"branding": "ブランディング", "inner_branding": "インナーブランディング"}


def _arrow(delta: float | None) -> str:
    if delta is None:
        return "→"
    if delta > 0:
        return "▲"
    if delta < 0:
        return "▼"
    return "→"


def _judgment_only(f: dict) -> str:
    """本文用: 状態（=judgment）のみ。確度（confidence）はAppendixにのみ表示する
    （ユーザーフィードバック①: Low確度の連呼はノイズ）。"""
    return f"[{f.get('judgment', '-')}]"


def _judgment_confidence(f: dict) -> str:
    """Appendix用: 状態＋確度の2軸表示。"""
    return f"[{f.get('judgment', '-')} / 確度: {f['confidence']}]"


# ── ① 今週の結論 ────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(●[^\n—-]*[^\n—\s-]|[A-F]\.[^\n—-]*[^\n—\s-])")


def _bold_heading(paragraph: str) -> str:
    """「●見出し」（または旧形式のA〜F.見出し）ラベルのみ太字にする。見出し直後の区切り（改行や「 — 」）は保持する。"""
    m = _HEADING_RE.match(paragraph)
    if not m:
        return paragraph
    heading = m.group(1)
    return f"**{heading}**{paragraph[len(heading):]}"


def _executive_summary_section(es: dict) -> str:
    narrative = es.get("narrative")
    if narrative:
        paragraphs = [p for p in narrative.strip().split("\n\n") if p.strip()]
    else:
        paragraphs = es["blocks"]
    return "\n\n".join(_bold_heading(p) for p in paragraphs) + "\n"


# ── ② サイトアクセス状況（GA4） ─────────────────────────────────────

def _site_usage_section(site_usage: dict) -> str:
    segments = site_usage.get("segments") or []
    if not segments:
        return "GA4データなし\n"

    header = "| セグメント | 今週 | 前期間比 |\n|---|---:|---:|\n"
    rows = []
    for seg in segments:
        if seg.get("error"):
            rows.append(f"| {seg['label']} | 取得失敗 | - |")
            continue
        delta_pct = seg.get("delta_pct")
        delta_str = f"{delta_pct:+.1f}%" if delta_pct is not None else "-"
        rows.append(f"| {seg['label']} | {seg.get('current', '-')} | {delta_str} |")
    return header + "\n".join(rows) + "\n"


# ── ③ 重点市場 ──────────────────────────────────────────────────

def _market_table(table: list[dict]) -> str:
    if not table:
        return "対象市場なし\n"
    header = "| 市場 | SEO状態 | 順位 | Imp | Click | AIO | 総合評価 |\n"
    header += "|---|---|---:|---:|---:|---|---|\n"
    rows = []
    for row in table:
        pos_str = f"{row['position']:.1f}位" if row["position"] is not None else "-"
        if row["delta_tier"] != "flat" and row["position_delta"] is not None:
            pos_str = f"{_arrow(row['position_delta'])} {pos_str}（{row['position_delta']:+.1f}）"
        rows.append(
            f"| {row['market_label']} | {row['seo_status']} | {pos_str} | {row['impressions']} | {row['clicks']} "
            f"| {row['aio_status']} | {row.get('judgment', '-')} |"
        )
    return header + "\n".join(rows) + "\n"


def _market_card(f: dict) -> str:
    evidence = f.get("evidence") or {}
    query = evidence.get("query", f["id"])
    region = _REGION_LABEL.get(evidence.get("region"), evidence.get("region") or "")
    market = _MARKET_LABEL.get(evidence.get("market"), evidence.get("market") or "")
    tokens = evidence.get("match_tokens") or []
    cur = evidence.get("current") or {}
    prv = evidence.get("comparison") or {}
    variants = evidence.get("matched_query_variants") or []
    aio = evidence.get("aio") or {}

    tcd_cited = aio.get("tcd_cited")
    tcd_str = "TCD引用あり" if tcd_cited is True else ("TCD未引用" if tcd_cited is False else "AIO評価対象外")
    domains = [d for d in (aio.get("cited_domains") or []) if d and d != "tcd.jp"]

    cur_pos, prv_pos, delta, tier = cur.get("position"), prv.get("position"), f.get("position_delta"), f.get("delta_tier", "flat")
    if cur_pos is None:
        pos_str = "-位"
    elif prv_pos is None or delta is None:
        pos_str = f"{cur_pos:.1f}位"
    elif tier == "flat":
        pos_str = f"{cur_pos:.1f}位（前週{prv_pos:.1f}位、{delta:+.1f}）"
    else:
        pos_str = f"{_arrow(delta)} {cur_pos:.1f}位（前週{prv_pos:.1f}位、{delta:+.1f}）"

    lines = [f"<details open><summary>{query}（{market} / {region}） {_judgment_only(f)}</summary>\n"]
    lines.append(f"- 一致条件（AND・語順不問）: {' + '.join(tokens)}")
    lines.append(f"- 順位: {pos_str}")
    lines.append(f"- Impressions: {cur.get('impressions', 0)}（前週{prv.get('impressions', 0)}） / Clicks: {cur.get('clicks', 0)}（前週{prv.get('clicks', 0)}）")
    lines.append(f"- {tcd_str}")
    lines.append(f"- AIO引用ドメイン（TCD以外）: {', '.join(domains) if domains else 'なし'}")
    for cf in f.get("competitor_findings", []):
        lines.append(f"- 競合動向: {cf['display_message'] or '-'}")
    if variants:
        variant_str = "、".join(f"「{v['query']}」(imp={v['impressions']})" for v in variants)
        lines.append(f"- 一致した実クエリ内訳（全件）: {variant_str}")
    lines.append(f"- {f['display_message'] or '-'}")
    lines.append(f"- 推奨: {f['recommendation']}")
    lines.append("\n</details>\n")
    return "\n".join(lines)


def _priority_markets_section(pm: dict) -> str:
    body = _market_table(pm["table"])
    body += "\n" + "\n".join(_market_card(c) for c in pm["cards"])
    return body


# ── ④ カテゴリ・ページ動向 ────────────────────────────────────────

def _category_summary_table(summary: list[dict]) -> str:
    if not summary:
        return "対象ページなし\n"
    header = "| カテゴリ | Imp変化 | Click変化 |\n|---|---|---|\n"
    rows = []
    for s in summary:
        cur, prv = s["current"], s["comparison"]
        click_delta = cur["clicks"] - prv["clicks"]
        rows.append(f"| {s['label']} | {prv['impressions']}→{cur['impressions']} | {prv['clicks']}→{cur['clicks']}（{click_delta:+d}） |")
    return header + "\n".join(rows) + "\n"


def _fmt_num(n) -> str:
    return "-" if n is None else str(n)


def _fmt_position(evidence: dict) -> str:
    cur_pos = (evidence or {}).get("current", {}).get("position")
    prv_pos = (evidence or {}).get("comparison", {}).get("position")
    cur_str = f"{cur_pos:.1f}" if cur_pos is not None else "-"
    prv_str = f"{prv_pos:.1f}" if prv_pos is not None else "-"
    delta = None if cur_pos is None or prv_pos is None else prv_pos - cur_pos
    return f"{_arrow(delta)} {prv_str}→{cur_str}"


def _page_label(f: dict) -> str:
    evidence = f.get("evidence") or {}
    return evidence.get("title") or evidence.get("page_id") or f["id"]


def _pages_table(findings: list[dict], show_confidence: bool = False) -> str:
    if not findings:
        return "該当ページなし\n"
    status_col = "状態・確度" if show_confidence else "状態"
    header = f"| ページ | {status_col} | Impressions | Clicks | 順位(前週→今週) | メモ |\n"
    header += "|---|---|---:|---:|---|---|\n"
    rows = []
    for f in findings:
        evidence = f.get("evidence") or {}
        cur = evidence.get("current", {})
        note = f["display_message"] or "-"
        if len(note) > 40:
            note = note[:40] + "…"
        status_str = _judgment_confidence(f) if show_confidence else _judgment_only(f)
        rows.append(
            f"| {_page_label(f)} | {status_str} | {_fmt_num(cur.get('impressions'))} "
            f"| {_fmt_num(cur.get('clicks'))} | {_fmt_position(evidence)} | {note} |"
        )
    return header + "\n".join(rows) + "\n"


def _category_page_trends_section(cpt: dict, low_impressions_threshold: int) -> str:
    notable = cpt["notable"]
    body = "### カテゴリ別\n\n" + _category_summary_table(cpt["category_summary"])
    body += "\n### 要確認ページ\n\n"
    body += f"（Impressions・順位差・クリック差が一定以上のページ、または重点市場の主対象URLのみ。※ Impressions {low_impressions_threshold}未満は参考値のため、それ以外はAppendix参照）\n\n"
    body += "#### 上昇\n\n" + _pages_table(notable["improved"])
    body += "\n#### 下落\n\n" + _pages_table(notable["declined"])
    return body


# ── ⑤ 施策評価 ──────────────────────────────────────────────────

def _initiative_row(f: dict, show_confidence: bool = False) -> str:
    status_str = _judgment_confidence(f) if show_confidence else _judgment_only(f)
    return f"- **{status_str}** {f['display_message'] or '-'}\n  - 推奨: {f['recommendation']}"


def _initiatives_section(initiatives: dict) -> str:
    known = initiatives["known"]
    unknown_count = initiatives["unknown_count"]
    parts = []
    if unknown_count:
        parts.append(f"施策評価: 実装日未登録の{unknown_count}施策があるため評価不能（詳細はAppendix）\n")
    if known:
        parts.append("\n".join(_initiative_row(f) for f in known))
    elif not unknown_count:
        parts.append("評価対象の施策なし")
    return "\n".join(parts) + "\n"


# ── ⑥ 今週やること ──────────────────────────────────────────────

def _this_week_action_row(f: dict, i: int) -> str:
    return (
        f"{i}. **{f['action_target']}** — {f['display_message'] or '-'}\n"
        f"   - 対応: {f['recommendation']}\n"
        f"   - {_judgment_only(f)}"
    )


def _this_week_actions_section(actions: list[dict]) -> str:
    if not actions:
        return "今週追加で実施すべきアクションはなし（継続観測中心）。\n"
    return "\n".join(_this_week_action_row(f, i) for i, f in enumerate(actions, start=1)) + "\n"


# ── Appendix ────────────────────────────────────────────────────

def _appendix_landing_pages(landing_pages: dict) -> str:
    top = landing_pages["top"]
    excluded = landing_pages["excluded"]
    if not top:
        body = "対象ページなし\n"
    else:
        header = "| 順位 | URL | Impressions | Clicks |\n|---:|---|---:|---:|\n"
        rows = [
            f"| {(f.get('evidence') or {}).get('rank', '-')} | {(f.get('evidence') or {}).get('url', f['id'])} "
            f"| {(f.get('evidence') or {}).get('current', {}).get('impressions', 0)} "
            f"| {(f.get('evidence') or {}).get('current', {}).get('clicks', 0)} |"
            for f in top
        ]
        body = header + "\n".join(rows) + "\n"

    if excluded:
        body += "\n除外URL一覧:\n"
        for e in excluded:
            reason = f"（{e['reason']}）" if e.get("reason") else ""
            body += f"- {e['url']}{reason}\n"
    return body


def _appendix_section(appendix: dict, low_impressions_threshold: int) -> str:
    dq = appendix["data_quality"]
    parts = ["## Appendix　※クリックして表示\n"]

    parts.append("### データ品質\n")
    parts.append(
        f"- データ品質: {dq.get('status', '-')}\n"
        f"- GSC取得件数: クエリ{dq.get('gsc_counts', {}).get('query_daily_rows', 0)}件 / "
        f"ページ{dq.get('gsc_counts', {}).get('page_daily_rows', 0)}件 / "
        f"ページ×クエリ{dq.get('gsc_counts', {}).get('page_query_daily_rows', 0)}件\n"
        f"- AIO取得状況: {dq.get('aio_status', {})}\n"
        f"- Finding件数: {dq.get('finding_count', 0)}件 {dq.get('severity_breakdown', {})}\n"
        f"- Confidence内訳: {dq.get('confidence_breakdown', {})}\n"
    )
    if dq.get("errors") or dq.get("warnings"):
        parts.append(f"- データ品質errors: {dq.get('errors', [])}\n- データ品質warnings: {dq.get('warnings', [])}\n")

    parts.append(f"\n### 少数データ・要確認以外のページ（{len(appendix['other_pages'])}件、※ Impressions {low_impressions_threshold}未満は参考値）\n")
    parts.append(_pages_table(appendix["other_pages"], show_confidence=True))

    parts.append("\n### 実装日未登録の施策\n")
    if appendix["unknown_initiatives"]:
        parts.append("\n".join(_initiative_row(f, show_confidence=True) for f in appendix["unknown_initiatives"]) + "\n")
    else:
        parts.append("なし\n")

    parts.append("\n### 上位ランディングページ（/branding/、定点観測対象外）\n")
    parts.append(_appendix_landing_pages(appendix["landing_pages"]))

    return "\n".join(parts)


def render_markdown(vm: dict) -> str:
    header = vm["header"]
    period = header["period"]
    low_impressions_threshold = header["low_impressions_threshold"]

    parts: list[str] = []
    parts.append(f"# TCD週次SEO/AIOレポート（{period['cur_start']} 〜 {period['cur_end']}）\n")
    parts.append(
        f"対象期間: {period['cur_start']}〜{period['cur_end']}（データ確定基準: 実行日-3日オフセット） ／ "
        f"データ品質: {header['data_quality_status']} ／ 継続週数: {header.get('weeks_available', '-')}週\n"
    )

    parts.append("## ① サイトアクセス状況\n")
    parts.append(_site_usage_section(vm["site_usage"]))

    parts.append("## ② 今週の結論（Executive Summary）\n")
    parts.append(_executive_summary_section(vm["executive_summary"]))

    parts.append("## ③ 今週やること\n")
    parts.append(_this_week_actions_section(vm["this_week_actions"]))

    parts.append("## ④ 重点市場\n")
    parts.append(_priority_markets_section(vm["priority_markets"]))

    parts.append("## ⑤ カテゴリ別アクセス動向\n")
    parts.append(_category_page_trends_section(vm["category_page_trends"], low_impressions_threshold))

    parts.append("## ⑥ 施策評価\n")
    parts.append(_initiatives_section(vm["initiatives"]))

    parts.append(_appendix_section(vm["appendix"], low_impressions_threshold))

    return "\n".join(parts) + "\n"
