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


def _summary_md(su: dict) -> list[str]:
    """① 冒頭のサマリー（HTMLの6カードに対応する表）。"""
    out = [f"**サマリー（指標: GA4 {su['metric_label']}・人）**\n", "| 項目 | 今週 | 前週比 |", "|---|---:|---:|"]
    for row in su.get("summary_tiles") or []:
        for s in row:
            if s.get("error"):
                out.append(f"| {s['label']} | 取得失敗 | - |")
            else:
                flag = "※異常値を含む" if s.get("anomalies") else _pct_label(s["delta_pct"])
                out.append(f"| {s['label']} | {_num(s['current'])} | {flag} |")
    return out + [""]


def _site_usage_v2(su: dict) -> str:
    out = [f"**到達ユーザー（指標: GA4 {su['metric_label']}）**\n",
           f"| セグメント | 集計対象（ページ） | 今週（人） | 前週（人） | 前週比 |\n|---|---|---:|---:|---:|"]
    for row in su["rows"]:
        for s in row:
            if s.get("error"):
                out.append(f"| {s['label']} | {s.get('rule_label') or '-'} | 取得失敗 | - | - |")
                continue
            flag = "※異常値を含む" if s.get("anomalies") else _pct_label(s["delta_pct"])
            out.append(f"| {s['label']} | {s.get('rule_label') or '-'} | {_num(s['current'])} | {_num(s['comparison'])} | {flag} |")
    out.append("")
    out += [f"- {n}" for n in [su["metric_note"], *su.get("rule_notes", []),
                                *(f"異常値（原因未確認）: {a}" for a in su["anomaly_notes"]), *su["extra_notes"]]]

    ch = su.get("channels")
    if ch:
        out.append(f"\n**流入元別（指標: GA4 {'セッション数'}）**\n")
        if ch["status"] != "ok":
            out.append(f"取得失敗（{ch.get('error') or '-'}）")
        else:
            out.append("| 流入元 | 今週（セッション） | 前週（セッション） | 前週比 | 構成比 | エンゲージメント率 |\n|---|---:|---:|---:|---:|---:|")
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
            out.append("| 週 | ユーザー 全体（人） | ユーザー 広告を除く（人） | セッション 全体 | うち広告 | セッション 広告を除く |")
            out.append("|---|---:|---:|---:|---:|---:|")
            for r in tr["rows"]:
                out.append(f"| {r['week_label']} | {r['users_total']:,} | {r['users_non_ad']:,} | {r['sessions_total']:,} "
                           f"| {r['sessions_ad']:,} | {r['sessions_non_ad']:,} |")
            out.append("")
            out.append(f"- {su['trend_note']}")
    st = su.get("segment_trend")
    if st and st["rows"]:
        out.append(f"\n**セグメント別の推移（指標: GA4 {su['metric_label']}・直近8週・新しい集計条件で再集計）**\n")
        out.append("| セグメント（人） | " + " | ".join(st["week_labels"]) + " |")
        out.append("|---|" + "---:|" * len(st["week_labels"]))
        for r in st["rows"]:
            cells = ["取得失敗"] * len(st["week_labels"]) if r["error"] else [_num(v) for v in r["values"]]
            out.append(f"| {r['label']} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("- 各週（月〜日）にそのページを1回以上表示したユーザー数。最新週は速報値。")
    return "\n".join(out) + "\n"


def _site_usage_section(site_usage: dict) -> str:
    if site_usage.get("scheme") == "v2":
        # 現行方式の①はサマリーだけ。詳細は render_markdown が「アクセスの詳細」として別の見出しで出す
        return "\n".join(_summary_md(site_usage)) + "\n"
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
    metric = f"（主指標: {item['primary_metric_label']}）" if item["primary_metric_label"] else "（評価方法: 正規化・インデックス確認）"
    lines = [f"#### {item['name']}\n", f"**判定: {item['verdict']}**{metric}\n"]
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
    lines += [f"- 判定の理由: {r}" for r in item["reasons"]] or ["- 判定の理由: -"]
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
    if vm.get("layout") == "content":
        return render_markdown_content(vm)
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
    if vm["site_usage"].get("scheme") == "v2":
        parts.append(f"## アクセスの詳細{sec['site_usage']}\n")
        parts.append(_site_usage_v2(vm["site_usage"]))
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


# ══ SEO/AIO・コンテンツ評価中心の構成（2026-11-23 再設計、vm["layout"] == "content"） ══

def _pos1(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def _ctr1(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _cell(text) -> str:
    return "—" if not text else str(text).replace("|", "／").replace("\n", " ")


def _page_cell(p: dict) -> str:
    return f"{p['short_title']}（{p['path']}）"


def _lead_md(c: dict) -> str:
    lead = c["lead"]
    out = ["**最重要の変化**\n"] + [f"- {h}" for h in lead["headline"]] + [""]
    out.append("**前週比で順位が上昇したコンテンツ**\n")
    if lead["progressed"]:
        out += ["| コンテンツ | 事実（前週→今週） | 評価 | 次の対応 |", "|---|---|---|---|"]
        for p in lead["progressed"]:
            ev = p["evaluation"] + ("。" + p["notes"][0] if p["notes"] else "")
            out.append(f"| {_cell(_page_cell(p))} | {p['facts']} | {_cell(ev)} | {_cell(p['next_action'])} |")
    else:
        out.append("該当なし")
    out.append("\n**確認が必要なコンテンツ（改善候補）**\n")
    if lead["candidates"]:
        out += ["| 区分 | コンテンツ | 事実（前週→今週） | 評価 | 確認すべき論点（未確認） | 次の対応 |", "|---|---|---|---|---|---|"]
        for p in lead["candidates"]:
            ev = p["evaluation"] + ("。" + p["notes"][0] if p["notes"] else "")
            out.append(f"| {p['status']} | {_cell(_page_cell(p))} | {p['facts']} | {_cell(ev)} | {_cell('／'.join(p['points']))} "
                       f"| {_cell(p['next_action'])} |")
    else:
        out.append("該当なし")
    out.append("")
    if lead["thin_candidates"]:
        names = "、".join(f"「{p['short_title']}」" for p in lead["thin_candidates"])
        out.append(f"- 評価材料不足（表示が週50回未満）のサービス・地域・比較ページ{len(lead['thin_candidates'])}件も改善候補に含める: "
                   f"{names}。順位・クリックの変化は評価していない（③・Appendix）。")
    out.append("- 順位の上昇・低下は前週比（1週分）の順位変動で、施策の効果判定ではない（施策は④で実装前後を比較対照と比べる）。"
               "「確認すべき論点」は未確認の問いで、原因を判断したものではない。")
    if lead["kpi_line"]:
        out.append(f"- 結果指標（参考）: {lead['kpi_line']}（詳細は⑤・⑥）")
    return "\n".join(out) + "\n"


def _aio_md(c: dict) -> str:
    aio = c["aio"]
    out = [f"{aio['summary']}。\n",
           "| キーワード | AIO | TCDの引用 | TCDの推薦 | 競合の推薦 | 引用ドメイン（今回の件数／前回からの増減） | TCDで最も表示されたページ |",
           "|---|---|---|---|---|---|---|"]
    for r in aio["rows"]:
        comp_label = r["citation_count_label"]
        top = r["top_page"]
        top_label = f"{top['name']}（{_pos1(top['position'])}位）" if top else "—"
        out.append(f"| {r['market_label']} | {r['aio_generated_label']} | {r['tcd_cited_label']} | {r['tcd_recommended_label']} "
                   f"| {r['competitor_recommendation_label']} | {comp_label} | {_cell(top_label)} |")
    out.append("")
    for r in aio["rows"]:
        out.append(f"#### {r['market_label']}\n")
        out.append("観測事実")
        out += [f"- {x}" for x in r["facts"]]
        if r["points"]:
            out.append("\n確認する論点（未検証）")
            out += [f"- {x}" for x in r["points"]]
        out.append(f"\n次の対応: {r['next_action']}\n")
    if aio["cross_domains"]:
        out.append("- 複数キーワードで引用されたドメイン: " + "、".join(
            f"{d}（{'・'.join(qs)}）" for d, qs in aio["cross_domains"].items()))
    out += [f"- {n}" for n in aio["notes"]]
    return "\n".join(out) + "\n"


def _kw_page_md(p: dict) -> str:
    prev = (f"前週{p['prev_impressions']}回・{_pos1(p['prev_position'])}位" if p.get("prev_impressions") is not None else "前週表示なし")
    return f"{p['name']}（今週{p['impressions']}回・{_pos1(p['position'])}位／{prev}）"


def _seo_content_md(c: dict, seo_notes: list[str]) -> str:
    out = ["**重点キーワードで表示されたページ**\n",
           "| キーワード | 検索意図 | 平均順位（前週比） | 表示・クリック | 表示されたページ（上位3） | 評価 |",
           "|---|---|---|---|---|---|"]
    checks = []
    for r in c["keyword_pages"]:
        if r["tier"] == "reference":
            continue
        shown = r["position"] is not None and r["impressions"]
        pos = f"{r['position']:.1f}（{r['delta_label']}）" if shown else "—"
        pages = "、".join(_kw_page_md(p) for p in r["pages"][:3]) or "—"
        out.append(f"| {r['market_label']} | {r['intent_label']} | {pos} | {r['impressions']}・{r['clicks']} | {_cell(pages)} "
                   f"| {_cell(r['page_evaluation'])} |")
        if r["movement"]:
            checks.append(f"\n**「{r['market_label']}」の順位変動（前週比・1週）**: このキーワードに一致したクエリでの、ページ別の表示（事実）\n")
            checks += [f"- {x}" for x in r["movement"]]
            checks.append("- 次の対応: ページ×クエリで、どのクエリ・どのページの順位が変わったかを確認する。1週の変化のため、これだけで改修は決めない")
        if r["investigate"]:
            checks.append(f"\n**調査対象: 「{r['market_label']}」**\n")
            checks += [f"- 事実: {r['page_evaluation']}", f"- 次の対応: {r['investigate']}"]
    out += checks
    if c["regional_lps"]:
        out += ["\n**地域LPの検索実績（全クエリの合計）**\n",
                "| 区分 | ページ | 順位（前週→今週） | 表示 | クリック | CTR | 主要クエリ（上位3） |", "|---|---|---|---|---|---|---|"]
        for p in c["regional_lps"]:
            out.append(f"| {p['status']} | {_cell(_page_cell(p))} | {_pos1(p['prev_position'])}→{_pos1(p['position'])} "
                       f"| {p['prev_impressions']:,}→{p['impressions']:,} | {p['prev_clicks']}→{p['clicks']} "
                       f"| {_ctr1(p['prev_ctr'])}→{_ctr1(p['ctr'])} | {_cell('、'.join(p['top_queries']))} |")
        out.append("\n- 重点キーワードの値はそのキーワードに一致するクエリだけの集計。この表は地域LPに表示された全クエリの合計で、両者は一致しない。")
    out.append("\n**ページ別の評価**（監視対象ページのうち、表示回数が今週・前週とも50以上のページ）\n")
    out += ["| 区分 | ページ | 検索意図 | 主要クエリ | 順位（前週→今週） | 表示 | クリック | CTR | 評価・確認すべき論点・次の対応 |",
            "|---|---|---|---|---|---:|---:|---:|---|"]
    thin = [p for p in c["pages"] if p["status"] == "評価材料不足"]
    for p in c["pages"]:
        if p["status"] == "評価材料不足":
            continue
        ev = p["evaluation"] + (f"／論点: {'・'.join(p['points'])}" if p["points"] else "") + f"／次: {p['next_action']}"
        out.append(f"| {p['status']} | {_cell(_page_cell(p))} | {p['intent_label']} | {_cell(p['top_query'])} "
                   f"| {_pos1(p['prev_position'])}→{_pos1(p['position'])} | {p['impressions']:,} | {p['clicks']} | {_ctr1(p['ctr'])} | {_cell(ev)} |")
    out.append("")
    out.append(f"- 評価材料不足（表示50回未満の週がある）{len(thin)}件は、変化を評価せずAppendixの「ページ別評価（全件）」に掲載。"
               "サービス・地域・比較ページは改善候補として①にも挙げる。")
    out.append(f"- {c['status_rules']}")
    out.append("- 順位・表示・CTR・クリックは判断材料で、増減だけで良否を決めない。")
    out += [f"- {n}" for n in seo_notes]
    return "\n".join(out) + "\n"


def _initiatives_content_md(c: dict) -> str:
    ini = c["initiatives"]
    cnt = ini["counts"]
    out = [f"施策{ini['item_count']}件（表示{len(ini['rows'])}行）: 評価可能 {cnt['評価可能']}・要調査 {cnt['要調査']}・評価保留 {cnt['評価保留']}\n"]
    for r in ini["rows"]:
        out.append(f"**[{r['class']}｜{r['verdict_short']}] {'／'.join(r['names'])}**\n")
        out.append(f"- 実装日: {r['implemented_at'] or '—'}" + (f"（対象{r['url_count']}ページ）" if r["url_count"] else ""))
        out.append(f"- 改修内容・仮説: {r['hypothesis'] or '—'}")
        if r["merged"]:
            out.append("- 施策（個別。対象ページ・実装日・判定・数値がすべて同じため表示上まとめた）: "
                       + "、".join(f"{i['name']}（{i['id']}・{i['verdict']}）" for i in r["items"]))
        if r["window"]:
            out.append(f"- 比較期間: {r['window']['label']}（{r['window']['note']}）")
        if r["metric_lines"]:
            out += ["", "| 指標 | 対象ページ（実装前→直近） | 比較対照 | 比較対照との差 |", "|---|---|---|---|"]
            out += [f"| {x['label']}{'' if x['judged'] else '（参考）'} | {x['target']} | {x['control']} | {x['diff']} |"
                    for x in r["metric_lines"]]
            out.append("")
        elif r["reason"]:
            out.append(f"- 評価しない理由: {r['reason']}")
        if r["overlaps"]:
            out.append(f"- 重なる施策: {'・'.join(r['overlaps'])}")
        out.append(f"- 次に確認すること: {r['next_action'] or '—'}\n")
    out += [f"- {n}" for n in ini["notes"]]
    return "\n".join(out) + "\n"


def _inquiry_dashboard_md(c: dict) -> str:
    d = c["inquiry_dashboard"]
    out = ["| 項目 | 当週 | 直近4週累計 |", "|---|---:|---:|"]
    for t in d["tiles"]:
        cur = "未集計" if t["current"] is None else f"{t['current']:,}"
        cum = inquiry_cumulative_label(d["cumulative"], t["cumulative_4w"])
        label = f"**{t['label']}**" if t["primary"] else t["label"]
        out.append(f"| {label} | {cur} | {cum} |")
    out.append("")
    out.append(f"- {d['status_line']}。案件相談は週ごとの変動が大きいため、評価には累計を使う。")
    out += [f"- 集計の警告: {w}" for w in d["warnings"]]
    return "\n".join(out) + "\n"


def _appendix_pages_all_md(c: dict) -> str:
    out = ["| 区分 | ページ | 検索意図 | 主要クエリ（上位3） | 順位（前週→今週） | 表示（前週→今週） | クリック | 評価 |",
           "|---|---|---|---|---|---|---:|---|"]
    for p in c["pages"]:
        out.append(f"| {p['status']} | {_cell(_page_cell(p))} | {p['intent_label']} | {_cell('、'.join(p['top_queries']))} "
                   f"| {_pos1(p['prev_position'])}→{_pos1(p['position'])} | {p['prev_impressions']:,}→{p['impressions']:,} "
                   f"| {p['prev_clicks']}→{p['clicks']} | {_cell(p['evaluation'])} |")
    return "\n".join(out) + "\n"


def render_markdown_content(vm: dict) -> str:
    header = vm["header"]
    periods = header["periods"]
    sec = periods["sections"]
    notes = periods["notes"]
    c = vm["content"]
    parts = [f"# TCD週次SEO/AIOレポート（{periods['title']}）\n",
             f"{periods['header_note']} ／ データ品質: {header['data_quality_label']} ／ 継続週数: {header.get('weeks_available', '-')}週\n"]
    parts.append("## ① 今週のSEO/AIO・コンテンツ評価\n")
    parts.append(_lead_md(c))
    parts.append(f"## ② AIO評価・競合比較{sec['aio']}\n")
    parts.append(_aio_md(c))
    parts.append(f"## ③ SEO・コンテンツ評価{sec['seo']}\n")
    parts.append(_seo_content_md(c, [n.replace("改善・悪化の判定", "順位上昇・低下の判定") for n in vm["priority_markets"]["seo_notes"] + notes["seo"]]))
    parts.append("## ④ 実施施策と効果検証\n")
    parts.append(_initiatives_content_md(c))
    parts.append(f"## ⑤ 問い合わせ実績{sec['inquiries']}\n")
    parts.append(_inquiry_dashboard_md(c))
    parts.append(f"## ⑥ アクセスの詳細{sec['site_usage']}\n")
    parts.append(_site_usage_section(vm["site_usage"]))
    parts.append(_site_usage_v2(vm["site_usage"]))
    if notes["site_usage"]:
        parts.append("\n".join(f"- {n}" for n in notes["site_usage"]) + "\n")

    appendix = vm["appendix"]
    dq = appendix["data_quality"]
    parts.append("## Appendix　※クリックして表示\n")
    parts.append("### キーワード別詳細（クエリ内訳・AIO引用元・判定）\n")
    parts.append(_keyword_details_section(vm["priority_markets"]))
    parts.append("### ページ別評価（全件・データ不足を含む）\n")
    parts.append(_appendix_pages_all_md(c))
    parts.append(_initiatives_section(appendix))
    parts.append("\n### データ品質\n")
    parts.append(
        f"- データ品質: {dq.get('label', '-')}\n"
        f"- GSC取得件数: クエリ{dq.get('gsc_counts', {}).get('query_daily_rows', 0)}件 / "
        f"ページ{dq.get('gsc_counts', {}).get('page_daily_rows', 0)}件 / "
        f"ページ×クエリ{dq.get('gsc_counts', {}).get('page_query_daily_rows', 0)}件\n"
        f"- AIO取得状況: {dq.get('aio_status', {})}\n"
        f"- Finding件数: {dq.get('finding_count', 0)}件 {dq.get('severity_breakdown', {})}\n"
        f"- Confidence内訳: {dq.get('confidence_breakdown', {})}\n"
        + "".join(f"- {line}\n" for line in dq.get("extra_lines") or [])
    )
    parts.append("\n### 上位ランディングページ（/branding/、定点観測対象外）\n")
    parts.append(_appendix_landing_pages(appendix["landing_pages"]))
    return "\n".join(parts) + "\n"
