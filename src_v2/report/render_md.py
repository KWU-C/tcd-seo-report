"""View ModelからMarkdownを生成する。分析ロジックは追加しない（整形のみ）。

render_html.pyと同じView Model・同じセクション構成・同じ閾値フィルタを参照する
（Markdown/HTML間で内容差異が生じないようにするため）。
"""
from __future__ import annotations

from src_v2.report.view_model import RELATED_QUERIES_NOTE, inquiry_cumulative_label, judgment_label


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


# ── ③ 今週の要点 ────────────────────────────────────────────────

def _highlights_section(es: dict) -> str:
    narrative = es.get("narrative")
    if narrative:
        return "\n\n".join(p for p in narrative.strip().split("\n\n") if p.strip()) + "\n"
    return "\n".join(f"- {line}" for line in es["blocks"]) + "\n"


# ── ① サイト全体のアクセス状況（GA4） ─────────────────────────────────────

def _pct_label(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def _num(v) -> str:
    return "-" if v is None else f"{v:,}"


def _site_usage_v2(su: dict) -> str:
    out = [f"**到達ユーザー（指標: GA4 {su['metric_label']}）**\n",
           f"| セグメント | 集計対象（ページ） | 今週 | 前週 | 前週比 |\n|---|---|---:|---:|---:|"]
    for row in su["rows"]:
        for s in row:
            if s.get("error"):
                out.append(f"| {s['label']} | {s.get('rule_label') or '-'} | 取得失敗 | - | - |")
                continue
            flag = "※異常値を含む" if s.get("anomalies") else _pct_label(s["delta_pct"])
            out.append(f"| {s['label']} | {s.get('rule_label') or '-'} | {_num(s['current'])} | {_num(s['comparison'])} | {flag} |")
    out.append("")
    out += [f"- {n}" for n in [su["metric_note"], *(f"異常値（原因未確認）: {a}" for a in su["anomaly_notes"]), *su["extra_notes"]]]

    ch = su.get("channels")
    if ch:
        out.append(f"\n**流入元別（指標: GA4 {'セッション数'}）**\n")
        if ch["status"] != "ok":
            out.append(f"取得失敗（{ch.get('error') or '-'}）")
        else:
            out.append("| 流入元 | 今週 | 前週 | 前週比 | 構成比 | エンゲージメント率 |\n|---|---:|---:|---:|---:|---:|")
            for r in ch["rows"]:
                share = "-" if r["share_pct"] is None else f"{r['share_pct']:.1f}%"
                eng = "-" if r["engagement_rate"] is None else f"{r['engagement_rate']:.1f}%"
                out.append(f"| {r['label']} | {r['current']:,} | {r['comparison']:,} | {_pct_label(r['delta_pct'])} | {share} | {eng} |")
            tot = ch["total"]
            out.append(f"| 合計（サイト全体） | {tot['current']:,} | {tot['comparison']:,} | {_pct_label(tot['delta_pct'])} | 100.0% | - |")
            out.append("")
            out.append(f"- {su['channel_note']}")
            if ch.get("ai_services"):
                out.append("- 生成AIの内訳（今週／前週）: " + "、".join(
                    f"{s['service']} {s['current']}／{s['comparison']}" for s in ch["ai_services"]))
    tr = su.get("trend")
    if tr:
        out.append("\n**推移（直近8週・月〜日）**\n")
        if tr["status"] != "ok":
            out.append(f"取得失敗（{tr.get('error') or '-'}）")
        else:
            out.append("| 週 | アクティブユーザー数（全体） | アクティブユーザー数（広告を除く） | セッション数（全体） | うち広告 | セッション数（広告を除く） |")
            out.append("|---|---:|---:|---:|---:|---:|")
            for r in tr["rows"]:
                out.append(f"| {r['week_label']} | {r['users_total']:,} | {r['users_non_ad']:,} | {r['sessions_total']:,} "
                           f"| {r['sessions_ad']:,} | {r['sessions_non_ad']:,} |")
            out.append("")
            out.append(f"- {su['trend_note']}")
    return "\n".join(out) + "\n"


def _site_usage_section(site_usage: dict) -> str:
    if site_usage.get("scheme") == "v2":
        return _site_usage_v2(site_usage)
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


# ── ② 問い合わせ実績 ─────────────────────────────────────────────

def _count_label(value: int | None, missing: str = "未入力") -> str:
    """当週の件数。値が無い（None）週は0件と区別して「未入力」（現行方式は「未集計」）と表示する。"""
    return missing if value is None else f"{value:,}"


def _inquiries_section(inq: dict) -> str:
    cum4 = inq["cumulative"][4]
    weekly = "status_label" in inq
    missing = "未集計" if weekly else "未入力"
    status = f"集計状況: **{inq['status_label']}**\n\n" if weekly else ""
    header = status + "| 項目 | 当週 | 直近4週累計 |\n|---|---:|---:|\n"
    rows = [
        f"| {row['label']} | {_count_label(row['current'], missing)} | {inquiry_cumulative_label(cum4, row['cumulative_4w'])} |"
        for row in inq["rows"]
    ]
    rate = inq.get("rate")
    if rate:
        rows.insert(2, f"| {rate['label']} | {rate['current_label'] or missing} | {rate['cumulative_4w_label']} |")
    forms = ""
    if inq.get("forms") and inq["forms"].get("state"):
        f = inq["forms"]
        if f["state"] == "ok":
            forms = ("\nフォーム区分（当週）\n\n| 項目 | 一般 | BtoB | 不明 |\n|---|---:|---:|---:|\n"
                     + "\n".join(f"| {r['label']} | {r['general']} | {r['b2b']} | {r['unknown']} |" for r in f["rows"]) + "\n")
        else:
            forms = f"\nフォーム区分（当週）: {f['label']}\n"
    return header + "\n".join(rows) + "\n" + forms + "\n" + "\n".join(f"- {n}" for n in inq["notes"]) + "\n"


# ── ④ SEO検索順位 ───────────────────────────────────────────────

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


# ── ⑤ AIO引用状況 ───────────────────────────────────────────────

def _aio_table(pm: dict) -> str:
    rows = pm["table"]
    if not rows:
        return "対象キーワードなし\n"
    header = "| キーワード | AIO生成 | TCD引用 | TCD推薦 | 主な引用元 |\n|---|---|---|---|---|\n"
    lines = [
        f"| {row['market_label']} | {row['aio_generated_label']} | {row['tcd_cited_label']} "
        f"| {row['tcd_recommended_label']} | {row['aio_sources_label']} |"
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
        unknown_count = aio_view.get("aio_unknown_source_count") or 0
        listed = domains + ([f"ドメイン不明 {unknown_count}件"] if unknown_count else [])
        lines.append(f"- AIO引用元（全件・TCD以外）: {', '.join(listed) if listed else '引用元なし'}")
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


def _initiative_v2(item: dict) -> str:
    lines = [f"#### {item['name']}\n", f"**判定: {item['verdict']}**（主指標: {item['primary_metric_label']}）\n"]
    if item["elapsed_label"]:
        lines.append(f"- 経過日数: {item['elapsed_label']}")
    elif item.get("implemented_at"):
        lines.append(f"- 実装日: {item['implemented_at']}")
    if item["period_label"]:
        lines.append(f"- 比較期間: {item['period_label']}")
    if item["urls"]:
        lines.append(f"- 対象ページ: {len(item['urls'])}件" + (f"（{item['urls'][0]}）" if len(item["urls"]) == 1 else ""))
    if item["hypothesis"]:
        lines.append(f"- 仮説: {item['hypothesis']}")
    if item["metrics_rows"]:
        lines.append("\n数値の変化（対象ページ）と、同じ期間の比較対照（対象ページ以外）の変化\n")
        lines.append("| 指標 | 実装前 | 実装後 | 変化 | 比較対照の変化 |\n|---|---:|---:|---:|---:|")
        lines += [f"| {r['label']} | {r['pre']} | {r['post']} | {r['change']} | {r['control_change']} |" for r in item["metrics_rows"]]
        lines.append("")
    q = item.get("queries")
    if q:
        lines.append(f"- 検索クエリの構成: {q['counts_label']}。実装後の表示回数のうち、実装前にも表示されていたクエリの割合 {q['shared_label']}")
        if q["top"]:
            lines.append(f"  - 実装後の上位: {'、'.join(q['top'])}")
        if q["increased"]:
            lines.append(f"  - 増えたクエリ: {'、'.join(q['increased'])}")
        if q["decreased"]:
            lines.append(f"  - 減ったクエリ: {'、'.join(q['decreased'])}")
    lines.append(f"- 判定の理由: {'／'.join(item['reasons']) or '-'}")
    lines.append(f"- 対応: {item['recommendation']}")
    lines.append("")
    return "\n".join(lines)


def _initiatives_section(appendix: dict) -> str:
    ini = appendix.get("initiatives") or {"scheme": "legacy", "items": appendix["unknown_initiatives"], "notes": []}
    if ini["scheme"] != "v2":
        body = ("\n".join(_initiative_row(f, show_confidence=True) for f in ini["items"]) + "\n") if ini["items"] else "なし\n"
        return "\n### 実装日未登録の施策\n\n" + body
    parts = ["\n### 施策の効果確認（実装前後の比較）\n"]
    parts.append("\n".join(f"- {n}" for n in ini["notes"]) + "\n")
    parts += [_initiative_v2(item) for item in ini["items"]] or ["なし\n"]
    return "\n".join(parts)


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
    if dq.get("extra_lines"):
        parts.append("".join(f"- {line}\n" for line in dq["extra_lines"]))
    if dq.get("errors") or dq.get("warnings"):
        parts.append(f"- データ品質errors: {dq.get('errors', [])}\n- データ品質warnings: {dq.get('warnings', [])}\n")

    parts.append(f"\n### 少数データ・要確認以外のページ（{len(appendix['other_pages'])}件、※ Impressions {low_impressions_threshold}未満は参考値）\n")
    parts.append(_pages_table(appendix["other_pages"], show_confidence=True))

    parts.append(_initiatives_section(appendix))

    parts.append("\n### 上位ランディングページ（/branding/、定点観測対象外）\n")
    parts.append(_appendix_landing_pages(appendix["landing_pages"]))

    return "\n".join(parts)


def render_markdown(vm: dict) -> str:
    header = vm["header"]
    low_impressions_threshold = header["low_impressions_threshold"]

    periods = header["periods"]
    sec = periods["sections"]
    notes = periods["notes"]

    parts: list[str] = []
    parts.append(f"# TCD週次SEO/AIOレポート（{periods['title']}）\n")
    parts.append(
        f"{periods['header_note']} ／ "
        f"データ品質: {header['data_quality_label']} ／ 継続週数: {header.get('weeks_available', '-')}週\n"
    )

    parts.append(f"## ① サイト全体のアクセス状況{sec['site_usage']}\n")
    parts.append(_site_usage_section(vm["site_usage"]))
    if notes["site_usage"]:
        parts.append("\n".join(f"- {n}" for n in notes["site_usage"]) + "\n")

    parts.append(f"## ② 問い合わせ実績{sec['inquiries']}\n")
    parts.append(_inquiries_section(vm["inquiries"]))

    parts.append("## ③ 今週の要点\n")
    parts.append(_highlights_section(vm["executive_summary"]))

    parts.append(f"## ④ SEO検索順位{sec['seo']}\n")
    parts.append(_seo_table(vm["priority_markets"]))
    if notes["seo"]:
        parts.append("\n".join(f"- {n}" for n in notes["seo"]) + "\n")

    parts.append(f"## ⑤ AIO引用状況{sec['aio']}\n")
    parts.append(_aio_table(vm["priority_markets"]))

    parts.append(_appendix_section(vm["appendix"], vm["priority_markets"], low_impressions_threshold))

    return "\n".join(parts) + "\n"
