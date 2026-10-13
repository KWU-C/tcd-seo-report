"""View ModelからMarkdownを生成する。分析ロジックは追加しない（整形のみ）。

render_html.pyと同じView Model・同じセクション構成・同じ閾値フィルタを参照する
（Markdown/HTML間で内容差異が生じないようにするため）。
"""
from __future__ import annotations

from src_v2.report.view_model import RELATED_QUERIES_NOTE, judgment_label


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
    return f"[{judgment_label(f)}]"


def _judgment_confidence(f: dict) -> str:
    """Appendix用: 状態＋確度の2軸表示。"""
    return f"[{judgment_label(f)} / 確度: {f['confidence']}]"


# ── ② 今週の要点 ────────────────────────────────────────────────

def _highlights_section(es: dict) -> str:
    narrative = es.get("narrative")
    if narrative:
        return "\n\n".join(p for p in narrative.strip().split("\n\n") if p.strip()) + "\n"
    return "\n".join(f"- {line}" for line in es["blocks"]) + "\n"


# ── ① サイト全体のアクセス状況（GA4） ─────────────────────────────────────

def _site_usage_section(site_usage: dict) -> str:
    segments = site_usage.get("segments") or []
    if not segments:
        return "GA4データなし\n"

    header = "| セグメント | 今週 | 前週比 |\n|---|---:|---:|\n"
    rows = []
    for seg in segments:
        if seg.get("error"):
            rows.append(f"| {seg['label']} | 取得失敗 | - |")
            continue
        delta_pct = seg.get("delta_pct")
        delta_str = f"{delta_pct:+.1f}%" if delta_pct is not None else "-"
        rows.append(f"| {seg['label']} | {seg.get('current', '-')} | {delta_str} |")
    return header + "\n".join(rows) + "\n"


# ── ③ SEO検索順位 ───────────────────────────────────────────────

def _keyword_label(row: dict) -> str:
    return row["market_label"] + (f"（{RELATED_QUERIES_NOTE}）" if row.get("includes_related_queries") else "")


def _seo_table(pm: dict) -> str:
    rows = pm["table"]
    if not rows:
        return "対象キーワードなし\n"
    header = "| キーワード | 月間検索Vol.（参考） | 平均順位 | 前週比 | Imp | Click |\n|---|---:|---:|---:|---:|---:|\n"
    lines = []
    for row in rows:
        shown = row["position"] is not None and row["impressions"]
        pos_str = f"{row['position']:.1f}" if shown else "—（GSCで表示データなし）"
        lines.append(
            f"| {_keyword_label(row)} | {row['volume_label'] or '-'} | {pos_str} | {row['delta_label']} "
            f"| {row['impressions']} | {row['clicks']} |"
        )
    notes = "\n".join(f"- {n}" for n in pm["seo_notes"])
    return header + "\n".join(lines) + "\n\n" + notes + "\n"


# ── ④ AIO引用状況 ───────────────────────────────────────────────

def _aio_table(pm: dict) -> str:
    rows = pm["table"]
    if not rows:
        return "対象キーワードなし\n"
    header = "| キーワード | AIO生成 | TCD引用 | 主な引用元 |\n|---|---|---|---|\n"
    lines = [
        f"| {row['market_label']} | {row['aio_generated_label']} | {row['tcd_cited_label']} | {row['aio_sources_label']} |"
        for row in rows
    ]
    return header + "\n".join(lines) + "\n\n" + "\n".join(f"- {n}" for n in pm["aio_notes"]) + "\n"


# ── Appendix: キーワード別詳細（本文の表にある順位・Imp・Clickは繰り返さない） ──

def _market_card(f: dict) -> str:
    evidence = f.get("evidence") or {}
    tokens = evidence.get("match_tokens") or []
    exact = evidence.get("match_exact") or []
    variants = evidence.get("matched_query_variants") or []
    aio_view = f.get("aio_view") or {}
    domains = aio_view.get("aio_domains") or []

    lines = [f"#### {f.get('card_title') or ''}\n", f"{_judgment_only(f)}\n"]
    if exact:
        lines.append(f"- 一致条件（完全一致）: {' / '.join(exact)}")
    else:
        lines.append(f"- 一致条件（AND・語順不問）: {' + '.join(tokens)}")
    if variants:
        variant_str = "、".join(f"「{v['query']}」(imp={v['impressions']})" for v in variants)
        lines.append(f"- 一致した実クエリ内訳（全件）: {variant_str}")
    if aio_view.get("citation_unavailable"):
        lines.append("- AIO引用元（全件）: 取得不可（AIO本文・引用元を取得できず）")
    elif aio_view.get("aio_generated_label") == "あり":
        lines.append(f"- AIO引用元（全件・TCD以外）: {', '.join(domains) if domains else '引用元なし'}")
    for cf in f.get("competitor_findings", []):
        lines.append(f"- 競合動向: {cf['display_message'] or '-'}")
    lines.append(f"- 判定: {f['display_message'] or '-'}")
    lines.append(f"- 推奨: {f['recommendation']}")
    if f.get("reference_rows"):
        items = []
        for r in f["reference_rows"]:
            pos = f"・{r['position']:.1f}位" if r["position"] is not None else ""
            items.append(f"{r['market_label']} Vol.{r['volume_label'] or '-'}（Imp {r['impressions']}{pos}）")
        lines.append(f"- 参考：{' ／ '.join(items)}")
    lines.append("")
    return "\n".join(lines)


def _keyword_details_section(pm: dict) -> str:
    return "\n".join(_market_card(c) for g in pm["groups"] for c in g["cards"])


# ── ページ表示ヘルパー（重点市場カード・Appendixで使用） ──────────────

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


def _initiative_row(f: dict, show_confidence: bool = False) -> str:
    status_str = _judgment_confidence(f) if show_confidence else _judgment_only(f)
    return f"- **{status_str}** {f['display_message'] or '-'}\n  - 推奨: {f['recommendation']}"


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


def _appendix_section(appendix: dict, pm: dict, low_impressions_threshold: int) -> str:
    dq = appendix["data_quality"]
    parts = ["## Appendix　※クリックして表示\n"]

    parts.append("### キーワード別詳細（クエリ内訳・AIO引用元・判定）\n")
    parts.append(_keyword_details_section(pm))

    parts.append("### データ品質\n")
    parts.append(
        f"- データ品質: {dq.get('label', '-')}\n"
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
        f"データ品質: {header['data_quality_label']} ／ 継続週数: {header.get('weeks_available', '-')}週\n"
    )

    parts.append("## ① サイト全体のアクセス状況\n")
    parts.append(_site_usage_section(vm["site_usage"]))

    parts.append("## ② 今週の要点\n")
    parts.append(_highlights_section(vm["executive_summary"]))

    parts.append("## ③ SEO検索順位\n")
    parts.append(_seo_table(vm["priority_markets"]))

    parts.append("## ④ AIO引用状況\n")
    parts.append(_aio_table(vm["priority_markets"]))

    parts.append(_appendix_section(vm["appendix"], vm["priority_markets"], low_impressions_threshold))

    return "\n".join(parts) + "\n"
