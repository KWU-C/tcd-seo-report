"""View ModelからHTMLを生成する。分析ロジックは追加しない（整形のみ）。

render_md.pyと同じView Modelを参照し、同じFinding集合を表示する
（Markdown/HTML間で内容差異が生じないようにするため）。

レポート構成（5セクション + Appendix。2026-10-13改善指示書、2026-10-27に②問い合わせ実績を追加）:
① サイト全体のアクセス状況 → ② 問い合わせ実績 → ③ 今週の要点 → ④ SEO検索順位 → ⑤ AIO引用状況 → Appendix。
判断に必要な情報を本文に残し、根拠の全件データはAppendix（<details>）へ移す。
"""
from __future__ import annotations

import re
from html import escape

from src_v2.report.view_model import RELATED_QUERIES_NOTE, inquiry_cumulative_label, judgment_label

_SEVERITY_STATUS = {"critical": "critical", "warning": "warning", "info": "neutral"}


def _esc(v) -> str:
    return escape(str(v))


def _status_chip(label: str, state: str) -> str:
    """state: good | warning | serious | critical | neutral。
    色はドット（マーク）にのみ乗せ、テキストはink色のまま。
    """
    return f'<span class="status-chip status-{state}"><span class="dot"></span>{escape(label)}</span>'


def _judgment_chip(f: dict) -> str:
    """本文用: 状態（=judgment）のみ表示する。確度（confidence）はノイズになるため
    本文には出さず、Appendixでのみ表示する（ユーザーフィードバック①）。"""
    state = _SEVERITY_STATUS.get(f["severity"], "neutral")
    return _status_chip(judgment_label(f), state)


def _judgment_confidence_html(f: dict) -> str:
    """Appendix用: 状態（=judgment）＋確度（=confidence）の2軸表示。"""
    return f'{_judgment_chip(f)} <span class="pill">確度: {escape(f["confidence"])}</span>'


def _fmt1(n) -> str:
    return f"{n:.1f}" if isinstance(n, (int, float)) else "-"


def _fmt_num(n) -> str:
    return "-" if n is None else escape(str(n))


# ── ③ 今週の要点 ────────────────────────────────────────────────

# AIOでのTCD引用は最重要指標のため、本文中の該当文言を赤字・太字で強調する（2026-09-30ユーザー指示）。
# メール（Gmail）でも確実に効くよう、CSS変数ではなくインラインstyleで指定する。
_AIO_CITED_PHRASES = ("TCD AIO引用あり", "AIOでTCD引用あり", "AIO引用あり", "TCD引用あり", "TCD推薦あり")
_AIO_CITED_RE = re.compile("|".join(re.escape(p) for p in _AIO_CITED_PHRASES))
_AIO_CITED_STYLE = "color:#d03b3b;font-weight:700"


def _highlight_aio_cited(text: str) -> str:
    """textをエスケープし、「AIO引用あり」系の文言だけ赤字・太字にしたHTMLを返す。"""
    parts, last = [], 0
    for m in _AIO_CITED_RE.finditer(text):
        parts.append(escape(text[last:m.start()]))
        parts.append(f'<strong style="{_AIO_CITED_STYLE}">{escape(m.group(0))}</strong>')
        last = m.end()
    parts.append(escape(text[last:]))
    return "".join(parts)


def _highlights_html(es: dict) -> str:
    narrative = es.get("narrative")
    if narrative:
        # 空行(\n\n)を段落境界とする。単一の\nは同じ段落内の改行として扱う
        paragraphs = [p for p in narrative.strip().split("\n\n") if p.strip()]
        return "".join(f"<p>{_highlight_aio_cited(p)}</p>" for p in paragraphs)
    return '<ul class="highlights">' + "".join(f"<li>{_highlight_aio_cited(line)}</li>" for line in es["blocks"]) + "</ul>"


# ── ① サイト全体のアクセス状況（GA4） ─────────────────────────────────────

def _pct_html(v: float | None) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def _n(v) -> str:
    return "-" if v is None else f"{v:,}"


def _table_html(head: list[str], rows: list[list[str]], num_from: int = 1, extra_class: str = "") -> str:
    def cls(i: int) -> str:
        return ' class="num"' if i >= num_from else ""
    th = "".join(f"<th{cls(i)}>{escape(h)}</th>" for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f"<td{cls(i)}>{c}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    # 狭い画面（スマートフォン）では表だけを横にスクロールさせる（ページ全体をはみ出させない）
    return (f'<div class="table-scroll" style="overflow-x:auto;-webkit-overflow-scrolling:touch">'
            f'<table class="pages-table market-table{(" " + extra_class) if extra_class else ""}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>')


def _site_usage_v2_html(su: dict) -> str:
    rows = []
    for row in su["rows"]:
        for s in row:
            if s.get("error"):
                rows.append([escape(s["label"]), escape(s.get("rule_label") or "-"), "取得失敗", "-", "-"])
                continue
            change = ('<span class="muted">※異常値を含む</span>' if s.get("anomalies") else _pct_html(s["delta_pct"]))
            rows.append([escape(s["label"]), escape(s.get("rule_label") or "-"), _n(s["current"]), _n(s["comparison"]), change])
    html = [f"<h3>到達ユーザー（指標: GA4 {escape(su['metric_label'])}）</h3>",
            _table_html(["セグメント", "集計対象（ページ）", "今週（人）", "前週（人）", "前週比"], rows, num_from=2),
            _notes_html([su["metric_note"], *su.get("rule_notes", []),
                         *(f"異常値（原因未確認）: {a}" for a in su["anomaly_notes"]), *su["extra_notes"]])]
    ch = su.get("channels")
    if ch:
        html.append("<h3>流入元別（指標: GA4 セッション数）</h3>")
        if ch["status"] != "ok":
            html.append(f"<p class='empty'>取得失敗（{escape(ch.get('error') or '-')}）</p>")
        else:
            crow = [[escape(r["label"]), f"{r['current']:,}", f"{r['comparison']:,}", _pct_html(r["delta_pct"]),
                     "-" if r["share_pct"] is None else f"{r['share_pct']:.1f}%",
                     "-" if r["engagement_rate"] is None else f"{r['engagement_rate']:.1f}%"] for r in ch["rows"]]
            tot = ch["total"]
            crow.append(["<strong>合計（サイト全体）</strong>", f"{tot['current']:,}", f"{tot['comparison']:,}",
                         _pct_html(tot["delta_pct"]), "100.0%", "-"])
            html.append(_table_html(["流入元", "今週（セッション）", "前週（セッション）", "前週比", "構成比", "エンゲージメント率"], crow))
            notes = [su["channel_note"]]
            if ch.get("ai_services"):
                notes.append("生成AIの内訳（今週／前週）: " + "、".join(
                    f"{s['service']} {s['current']}／{s['comparison']}" for s in ch["ai_services"]))
            html.append(_notes_html(notes))
    tr = su.get("trend")
    if tr:
        html.append("<h3>推移（直近8週・月〜日）</h3>")
        if tr["status"] != "ok":
            html.append(f"<p class='empty'>取得失敗（{escape(tr.get('error') or '-')}）</p>")
        else:
            trows = [[escape(r["week_label"]), f"{r['users_total']:,}", f"{r['users_non_ad']:,}", f"{r['sessions_total']:,}",
                      f"{r['sessions_ad']:,}", f"{r['sessions_non_ad']:,}"] for r in tr["rows"]]
            html.append(_table_html(["週", "ユーザー 全体（人）", "ユーザー 広告を除く（人）",
                                     "セッション 全体", "うち広告", "セッション 広告を除く"], trows))
            html.append(_notes_html([su["trend_note"]]))
    st = su.get("segment_trend")
    if st and st["rows"]:
        html.append(f"<h3>セグメント別の推移（指標: GA4 {escape(su['metric_label'])}・直近8週・新しい集計条件で再集計）</h3>")
        srows = [[escape(r["label"])] + (["取得失敗"] * len(st["week_labels"]) if r["error"] else [_n(v) for v in r["values"]])
                 for r in st["rows"]]
        html.append(_table_html(["セグメント（人）", *st["week_labels"]], srows))
        html.append(_notes_html(["各週（月〜日）にそのページを1回以上表示したユーザー数。最新週は速報値。"]))
    return "".join(html)


def _site_usage_summary_html(su: dict) -> str:
    """① サイト全体のアクセス状況（6カードのサマリー）。詳細は別のセクション「アクセスの詳細」。"""
    return (_summary_tiles_html(su)
            + f'<p class="note">カードの数値: GA4 {escape(su["metric_label"])}（人）。前週比は新しい集計条件どうしの比較。'
              "詳細は下の「アクセスの詳細」。</p>")


def _site_usage_html(site_usage: dict) -> str:
    if site_usage.get("scheme") == "v2":
        return _site_usage_summary_html(site_usage)
    segments = site_usage.get("segments") or []
    if not segments:
        return "<p class='empty'>GA4データなし</p>"

    rows_html = []
    for row in site_usage.get("rows") or [segments]:
        rows_html.append(f'<div class="stat-row stat-row-4">{"".join(_site_tile_html(seg) for seg in row)}</div>')
    return "".join(rows_html)


def _summary_tiles_html(su: dict) -> str:
    """① 冒頭のサマリー（旧版の6カード形式）。値と前週比は詳細の表と同じセグメントの値。"""
    rows = su.get("summary_tiles") or []
    return "".join(f'<div class="stat-row stat-row-4">{"".join(_site_tile_html(seg, thousands=True) for seg in row)}</div>'
                   for row in rows)


def _site_tile_html(seg: dict, thousands: bool = False) -> str:
    if seg.get("error"):
        return f"""
        <div class="stat-tile">
          <div class="stat-label">{escape(seg['label'])}</div>
          <div class="stat-value-text">取得失敗</div>
        </div>"""

    delta_pct = seg.get("delta_pct")
    delta_html = ""
    if seg.get("anomalies"):
        # 詳細の表と同じく、異常値を含むセグメントは前週比を出さない
        delta_html = '<div class="stat-sub"><span class="muted">※異常値を含む（前週比は評価しない）</span></div>'
    elif delta_pct is not None:
        state = "good" if delta_pct >= 0 else "critical"
        delta_html = f'<div class="stat-sub">{_status_chip(f"{delta_pct:+.1f}%", state)}（前週比）</div>'
    value = _n(seg.get("current")) if thousands else _fmt_num(seg.get("current"))
    return f"""
        <div class="stat-tile">
          <div class="stat-label">{escape(seg['label'])}</div>
          <div class="stat-value-num">{value}</div>
          {delta_html}
        </div>"""


# ── ② 問い合わせ実績（config_v2/inquiries.yaml の確定済み件数） ──────────

def _inquiry_count_html(value: int | None, missing: str = "未入力") -> str:
    """当週の件数。値が無い（None）週は0件と区別して「未入力」（現行方式は「未集計」）と表示する。"""
    return f'<span class="muted">{missing}</span>' if value is None else f"{value:,}"


def _inquiry_cumulative_html(cum: dict, value: int | None) -> str:
    label = escape(inquiry_cumulative_label(cum, value))
    return label if cum["complete"] else f'<span class="muted">{label}</span>'


def _inquiries_html(inq: dict) -> str:
    cum4 = inq["cumulative"][4]
    weekly = "status_label" in inq
    missing = "未集計" if weekly else "未入力"
    status_html = ""
    if weekly:
        cls = "" if inq["entered"] else ' class="inquiry-status-missing"'
        status_html = f'<p class="note">集計状況: <strong{cls}>{escape(inq["status_label"])}</strong></p>'
    rows = []
    for row in inq["rows"]:
        # 案件相談は最終的な営業成果指標のため強調する
        label = f"<strong>{escape(row['label'])}</strong>" if row["key"] in ("general_total", "project_consultation") else escape(row["label"])
        rows.append(f"""
      <tr>
        <td>{label}</td>
        <td class="num">{_inquiry_count_html(row['current'], missing)}</td>
        <td class="num">{_inquiry_cumulative_html(cum4, row['cumulative_4w'])}</td>
      </tr>""")
    rate = inq.get("rate")
    if rate:
        rows.insert(2, f"""
      <tr>
        <td><strong>{escape(rate['label'])}</strong></td>
        <td class="num">{escape(rate['current_label']) if rate['current_label'] else f'<span class="muted">{missing}</span>'}</td>
        <td class="num">{escape(rate['cumulative_4w_label'])}</td>
      </tr>""")
    forms_html = ""
    forms = inq.get("forms") or {}
    if forms.get("state") == "ok":
        forms_html = "<h3>フォーム区分（当週）</h3>" + _table_html(
            ["項目", "一般", "BtoB", "不明"],
            [[escape(r["label"]), str(r["general"]), str(r["b2b"]), str(r["unknown"])] for r in forms["rows"]])
    elif forms.get("state"):
        forms_html = f'<p class="note">フォーム区分（当週）: <span class="muted">{escape(forms["label"])}</span></p>'
    return f"""{status_html}
    <table class="pages-table market-table inquiry-table">
      <thead>
        <tr><th>項目</th><th class="num">当週</th><th class="num">直近4週累計</th></tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    {forms_html}
    {_notes_html(inq["notes"])}"""


# ── ④ SEO検索順位（重点4キーワードを1つの表で表示） ──────────────────

def _notes_html(notes: list[str]) -> str:
    return '<ul class="table-notes">' + "".join(f"<li>{escape(n)}</li>" for n in notes) + "</ul>"


def _seo_table_html(pm: dict) -> str:
    if not pm["table"]:
        return "<p class='empty'>対象キーワードなし</p>"
    return f"""
    <table class="pages-table market-table">
      <thead>
        <tr><th>キーワード</th><th class="num">月間検索Vol.（参考）</th><th class="num">平均順位</th><th>前週比</th><th class="num">Imp</th><th class="num">Click</th></tr>
      </thead>
      <tbody>{"".join(_seo_row_html(row) for row in pm["table"])}</tbody>
    </table>
    {_notes_html(pm["seo_notes"])}"""


def _seo_row_html(row: dict) -> str:
    shown = row["position"] is not None and row["impressions"]
    pos_html = f'<span class="rank-value">{row["position"]:.1f}</span>' if shown else '— <span class="cell-note">GSCで表示データなし</span>'
    # 色は改善・悪化と判定できた場合のみ（参考値・横ばいは無彩色）
    state = {"改善": "good", "悪化": "critical"}.get(row["seo_movement"] if shown else "", "neutral")
    related = f'<span class="cell-note">{escape(RELATED_QUERIES_NOTE)}</span>' if row.get("includes_related_queries") else ""
    return f"""
    <tr>
      <td><strong>{escape(row['market_label'])}</strong>{related}</td>
      <td class="num muted">{escape(row['volume_label'] or '-')}</td>
      <td class="num">{pos_html}</td>
      <td>{_status_chip(row['delta_label'], state)}</td>
      <td class="num">{row['impressions']}</td>
      <td class="num">{row['clicks']}</td>
    </tr>"""


# ── ⑤ AIO引用状況（同じ4キーワード。SEO順位とは別の表にする） ──────────

def _aio_table_html(pm: dict) -> str:
    if not pm["table"]:
        return "<p class='empty'>対象キーワードなし</p>"
    return f"""
    <table class="pages-table market-table">
      <thead>
        <tr><th>キーワード</th><th>AIO生成</th><th>TCD引用</th><th>TCD推薦</th><th>主な引用元</th></tr>
      </thead>
      <tbody>{"".join(_aio_row_html(row) for row in pm["table"])}</tbody>
    </table>
    {_notes_html(pm["aio_notes"])}"""


def _aio_row_html(row: dict) -> str:
    tcd_label = row["tcd_cited_label"]
    if row["tcd_cited"]:
        tcd_html = f'<strong style="{_AIO_CITED_STYLE}">{escape(tcd_label)}</strong>'
    else:
        tcd_html = _status_chip(tcd_label, "warning" if tcd_label == "なし" else "neutral")
    rec_label = row["tcd_recommended_label"]
    if rec_label == "あり":
        rec_html = f'<strong style="{_AIO_CITED_STYLE}">{escape(rec_label)}</strong>'
    elif rec_label in ("なし", "判定不可"):
        rec_html = _status_chip(rec_label, "warning" if rec_label == "なし" else "neutral")
    else:
        rec_html = f'<span class="muted">{escape(rec_label)}</span>'
    sources_class = ' class="muted"' if row["citation_unavailable"] else ""
    return f"""
    <tr>
      <td><strong>{escape(row['market_label'])}</strong></td>
      <td>{escape(row['aio_generated_label'])}</td>
      <td>{tcd_html}</td>
      <td>{rec_html}</td>
      <td{sources_class}>{escape(row['aio_sources_label'])}</td>
    </tr>"""


# ── Appendix: キーワード別詳細（本文の表にある順位・Imp・Clickは繰り返さない） ──

def _market_card_html(f: dict) -> str:
    evidence = f.get("evidence") or {}
    tokens = evidence.get("match_tokens") or []
    exact = evidence.get("match_exact") or []
    variants = evidence.get("matched_query_variants") or []
    aio_view = f.get("aio_view") or {}
    cited_domains = aio_view.get("aio_domains") or []

    competitor_notes = ""
    for cf in f.get("competitor_findings", []):
        direction_state = "warning" if "new" in cf["id"] and cf["severity"] == "warning" else "neutral"
        competitor_notes += f'<div class="competitor-note">{_status_chip("競合動向", direction_state)} {escape(cf["display_message"] or "-")}</div>'

    if aio_view.get("citation_unavailable"):
        domain_chips = '<span class="chip chip-muted">取得不可（AIO本文・引用元を取得できず）</span>'
    elif aio_view.get("aio_generated_label") != "あり":
        domain_chips = f'<span class="chip chip-muted">AIO生成: {escape(aio_view.get("aio_generated_label") or "未取得")}</span>'
    else:
        unknown_count = aio_view.get("aio_unknown_source_count") or 0
        domain_chips = "".join(f'<span class="chip">{escape(d)}</span>' for d in cited_domains)
        if unknown_count:
            domain_chips += f'<span class="chip chip-muted">ドメイン不明 {unknown_count}件</span>'
        domain_chips = domain_chips or '<span class="chip chip-muted">引用元なし</span>'

    if variants:
        variant_items = "".join(
            f'<li>「{escape(v["query"])}」 imp={v["impressions"]} / clicks={v["clicks"]}</li>' for v in variants
        )
        variants_html = f'<ul class="variant-list">{variant_items}</ul>'
    else:
        variants_html = "<p class='empty'>一致した実クエリなし</p>"

    if exact:
        match_html = f'一致条件（完全一致）: {" / ".join(escape(t) for t in exact)}'
    else:
        match_html = f'一致条件（AND・語順不問）: {" + ".join(escape(t) for t in tokens)}'

    reference_html = ""
    if f.get("reference_rows"):
        items = []
        for r in f["reference_rows"]:
            pos = f"・{r['position']:.1f}位" if r["position"] is not None else ""
            items.append(f"{escape(r['market_label'])} Vol.{escape(r['volume_label'] or '-')}（Imp {r['impressions']}{pos}）")
        reference_html = f'<div class="reference-note">参考：{" ／ ".join(items)}</div>'

    return f"""
    <div class="query-card">
      <h4 class="card-title">{escape(f.get("card_title") or "")}</h4>
      <div class="card-judgment">{_judgment_chip(f)}</div>

      <div class="match-condition">{match_html}</div>

      <div class="variants">
        <div class="stat-label">一致した実クエリ内訳（全件）</div>
        {variants_html}
      </div>

      <div class="domains">
        <div class="stat-label">AIO引用元（全件・TCD以外）</div>
        <div class="chip-row">{domain_chips}</div>
      </div>

      {competitor_notes}

      <div class="verdict">
        <div class="message">{_highlight_aio_cited(f['display_message'] or '-')}</div>
        <div class="judgment-line">推奨: {escape(f['recommendation'])}</div>
      </div>
      {reference_html}
    </div>"""


def _keyword_details_html(pm: dict) -> str:
    cards = "".join(_market_card_html(c) for g in pm["groups"] for c in g["cards"])
    return f'<div class="card-grid">{cards}</div>' if cards else "<p class='empty'>対象キーワードなし</p>"


# ── ページ表示ヘルパー（重点市場カード・Appendixで使用） ──────────────

def _position_delta_chip(evidence: dict) -> str:
    cur_pos = (evidence or {}).get("current", {}).get("position")
    prv_pos = (evidence or {}).get("comparison", {}).get("position")
    if cur_pos is None or prv_pos is None:
        return _status_chip(f"{_fmt1(cur_pos)}位", "neutral")
    delta = prv_pos - cur_pos
    if delta == 0:
        return _status_chip(f"→ {prv_pos:.1f}→{cur_pos:.1f}位", "neutral")
    arrow = "▲" if delta > 0 else "▼"
    state = "good" if delta > 0 else "critical"
    return _status_chip(f"{arrow} {prv_pos:.1f}→{cur_pos:.1f}位（{delta:+.1f}）", state)


def _page_label(f: dict) -> str:
    evidence = f.get("evidence") or {}
    return evidence.get("title") or evidence.get("page_id") or f["id"]


def _pages_table_html(findings: list[dict], show_confidence: bool = False) -> str:
    if not findings:
        return "<p class='empty'>該当ページなし</p>"

    status_th = "状態・確度" if show_confidence else "状態"
    rows = []
    for f in findings:
        evidence = f.get("evidence") or {}
        cur = evidence.get("current", {})
        note = f["display_message"] or "-"
        if len(note) > 60:
            note = note[:60] + "…"
        status_html = _judgment_confidence_html(f) if show_confidence else _judgment_chip(f)
        rows.append(f"""
        <tr>
          <td>{escape(_page_label(f))}</td>
          <td>{status_html}</td>
          <td class="num">{_fmt_num(cur.get('impressions'))}</td>
          <td class="num">{_fmt_num(cur.get('clicks'))}</td>
          <td>{_position_delta_chip(evidence)}</td>
          <td>{escape(note)}</td>
        </tr>""")

    return f"""
    <table class="pages-table">
      <thead>
        <tr><th>ページ</th><th>{status_th}</th><th>Impressions</th><th>Clicks</th><th>順位(前週→今週)</th><th>メモ</th></tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


def _initiative_html(f: dict, show_confidence: bool = False) -> str:
    status_html = _judgment_confidence_html(f) if show_confidence else _judgment_chip(f)
    return f"""
    <li class="finding">
      {status_html}
      <div class="message">{escape(f['display_message'] or '-')}</div>
      <div class="judgment">推奨: {escape(f['recommendation'])}</div>
    </li>"""


# 判定は観測の分類で、良し悪しの評価ではないため、色（良い・悪い）を付けない
_VERDICT_STATE: dict[str, str] = {}


def _initiative_v2_html(item: dict) -> str:
    facts = []
    if item["elapsed_label"]:
        facts.append(f"経過日数: {item['elapsed_label']}")
    elif item.get("implemented_at"):
        facts.append(f"実装日: {item['implemented_at']}")
    if item["period_label"]:
        facts.append(f"比較期間: {item['period_label']}")
    if item["urls"]:
        facts.append(f"対象ページ: {len(item['urls'])}件" + (f"（{item['urls'][0]}）" if len(item["urls"]) == 1 else ""))
    if item["hypothesis"]:
        facts.append(f"仮説: {item['hypothesis']}")
    table = ""
    if item["metrics_rows"]:
        table = ("<p class='note'>数値の変化（対象ページ）と、同じ期間の比較対照（対象ページ以外）の変化</p>"
                 + _table_html(["指標", "実装前", "実装後", "変化", "比較対照の変化"],
                               [[escape(r["label"]), escape(r["pre"]), escape(r["post"]), escape(r["change"]),
                                 escape(r["control_change"])] for r in item["metrics_rows"]]))
    q = item.get("queries")
    qlines = []
    if q:
        qlines.append(f"検索クエリの構成: {q['counts_label']}。実装後の表示回数のうち、実装前にも表示されていたクエリの割合 {q['shared_label']}")
        for label, key in (("実装後の上位", "top"), ("増えたクエリ", "increased"), ("減ったクエリ", "decreased")):
            if q[key]:
                qlines.append(f"{label}: {'、'.join(q[key])}")
    return f"""
    <div class="initiative">
      <h4>{escape(item['name'] or '-')}</h4>
      <p>{_status_chip(f"判定: {item['verdict']}", _VERDICT_STATE.get(item['verdict'], 'neutral'))}
         <span class="pill">{escape(f"主指標: {item['primary_metric_label']}" if item['primary_metric_label'] else "評価方法: 正規化・インデックス確認")}</span></p>
      {_notes_html(facts)}
      {table}
      {_notes_html(qlines) if qlines else ""}
      {_notes_html([*(f"判定の理由: {r}" for r in item['reasons']), f"対応: {item['recommendation']}"])}
    </div>"""


def _initiatives_html(appendix: dict) -> tuple[str, str]:
    ini = appendix.get("initiatives") or {"scheme": "legacy", "items": appendix["unknown_initiatives"], "notes": []}
    if ini["scheme"] != "v2":
        body = ("<ul class=\"findings\">" + "".join(_initiative_html(f, show_confidence=True) for f in ini["items"]) + "</ul>"
                if ini["items"] else "<p class='empty'>なし</p>")
        return "実装日未登録の施策", body
    body = _notes_html(ini["notes"]) + ("".join(_initiative_v2_html(i) for i in ini["items"]) or "<p class='empty'>なし</p>")
    return "施策の効果確認（実装前後の比較）", body


# ── Appendix ────────────────────────────────────────────────────

def _appendix_data_quality_html(dq: dict) -> str:
    gsc = dq.get("gsc_counts", {})
    aio = dq.get("aio_status", {})
    conf = dq.get("confidence_breakdown", {})
    sev = dq.get("severity_breakdown", {})
    errors_warnings = ""
    if dq.get("errors") or dq.get("warnings"):
        errors_warnings = f"<li>データ品質errors: {escape(str(dq.get('errors', [])))}</li><li>データ品質warnings: {escape(str(dq.get('warnings', [])))}</li>"
    return f"""
    <ul>
      <li>データ品質: {escape(dq.get('label', '-'))}</li>
      <li>GSC取得件数: クエリ{gsc.get('query_daily_rows', 0)} / ページ{gsc.get('page_daily_rows', 0)} / ページ×クエリ{gsc.get('page_query_daily_rows', 0)}</li>
      <li>AIO取得状況: {escape(str(aio))}</li>
      <li>Finding件数: {dq.get('finding_count', 0)}件（{escape(str(sev))}）</li>
      <li>Confidence内訳: {escape(str(conf))}</li>
      {"".join(f"<li>{escape(line)}</li>" for line in dq.get("extra_lines", []))}
      {errors_warnings}
    </ul>"""


def _appendix_landing_pages_html(landing_pages: dict) -> str:
    top = landing_pages["top"]
    excluded = landing_pages["excluded"]
    if not top:
        table_html = "<p class='empty'>対象ページなし</p>"
    else:
        rows = []
        for f in top:
            evidence = f.get("evidence") or {}
            cur = evidence.get("current", {})
            rows.append(f"""
        <tr>
          <td class="num">{evidence.get('rank', '-')}</td>
          <td>{escape(evidence.get('url', f['id']))}</td>
          <td class="num">{cur.get('impressions', 0)}</td>
          <td class="num">{cur.get('clicks', 0)}</td>
        </tr>""")
        table_html = f"""
    <table class="pages-table">
      <thead><tr><th>順位</th><th>URL</th><th>Impressions</th><th>Clicks</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""

    if excluded:
        excluded_lines = []
        for e in excluded:
            reason_suffix = f"（{escape(e['reason'])}）" if e.get("reason") else ""
            excluded_lines.append(f"<li>{escape(e['url'])}{reason_suffix}</li>")
        excluded_html = f"<div class='excluded-list'>除外URL一覧:<ul>{''.join(excluded_lines)}</ul></div>"
    else:
        excluded_html = ""

    return table_html + excluded_html


def _appendix_html(appendix: dict, pm: dict, header: dict) -> str:
    other_pages_html = _pages_table_html(appendix["other_pages"], show_confidence=True)
    initiatives_title, initiatives_html = _initiatives_html(appendix)
    all_findings_html = "<p class='note'>全" + str(len(appendix["all_findings"])) + "件（severity/confidence順）</p>" + _pages_table_html(
        [f for f in appendix["all_findings"] if f["category"] == "page"], show_confidence=True,
    )

    return f"""
    <details class="appendix">
      <summary>Appendix　※クリックして表示</summary>

      <h3>キーワード別詳細（クエリ内訳・AIO引用元・判定）</h3>
      {_keyword_details_html(pm)}

      <h3>データ品質</h3>
      {_appendix_data_quality_html(appendix["data_quality"])}

      <h3>少数データ・要確認以外のページ（{len(appendix["other_pages"])}件、※ Impressions {header['low_impressions_threshold']}未満は参考値）</h3>
      {other_pages_html}

      <h3>{escape(initiatives_title)}</h3>
      {initiatives_html}

      <h3>上位ランディングページ（/branding/、定点観測対象外）</h3>
      {_appendix_landing_pages_html(appendix["landing_pages"])}

      <h3>全Finding（ページカテゴリの再掲・監査用）</h3>
      {all_findings_html}
    </details>"""


_STYLE = """
<style>
  :root {
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page-plane:      #e0e0cf;
    --market-surface:  #efefe5;
    --chip-surface:    #f7f7f2;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --gridline:       #e0e0cf;
    --border:         rgba(11,11,11,0.10);
    --status-good:     #0ca30c;
    --status-warning:  #fab219;
    --status-serious:  #ec835a;
    --status-critical: #d03b3b;
    --status-neutral:  #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page-plane:      #0d0d0d;
      --market-surface:  #0d0d0d;
      --chip-surface:    #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --gridline:       #2c2c2a;
      --border:         rgba(255,255,255,0.10);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --page-plane:      #0d0d0d;
    --market-surface:  #0d0d0d;
    --chip-surface:    #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --gridline:       #2c2c2a;
    --border:         rgba(255,255,255,0.10);
  }

  html, body {
    margin: 0;
    background: var(--page-plane);
  }

  .report-root {
    font-family: system-ui, -apple-system, "Hiragino Kaku Gothic ProN", "Segoe UI", sans-serif;
    max-width: 1080px; margin: 0 auto; padding: 2rem 1rem 4rem;
    background: var(--page-plane); color: var(--text-primary); line-height: 1.7;
  }
  h1 { font-size: 1.5rem; border-bottom: 2px solid var(--text-primary); padding-bottom: .6rem; margin-bottom: 1rem; }
  h2 { font-size: 1.2rem; margin: 2.2rem 0 1rem; padding-bottom: .3rem; border-bottom: 1px solid var(--gridline); }
  h3 { font-size: 1rem; margin: 1.4rem 0 .6rem; }
  h4 { font-size: .95rem; margin: 1.2rem 0 .4rem; }
  .initiative { border-top: 1px solid var(--border); padding-top: .4rem; margin-top: 1rem; }
  h4 { font-size: .9rem; margin: 1rem 0 .4rem; color: var(--text-secondary); }
  /* 帯表示（Appendix等で使用） */
  .trend-band { background: var(--market-surface); border-radius: 8px; padding: .8rem 1rem 1.1rem; margin-bottom: 1rem; }
  .trend-band h4 { margin-top: 0; }
  .trend-band table.pages-table { background: var(--surface-1); border-radius: 6px; padding: .4rem .6rem; }
  section { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.4rem; margin-bottom: 1.4rem; }
  .executive-summary p { margin: 0; padding: .7rem 0; white-space: pre-line; }
  .executive-summary p + p { border-top: 1px solid var(--border); }
  section > h2 { margin-top: 0; }
  .lede { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.4rem; margin: 0 0 1.4rem; font-size: .9rem; color: var(--text-secondary); }

  /* stat row / stat tile */
  .stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .8rem; }
  /* ① サイトアクセス状況: 4列固定（2段目の問い合わせ・資料請求も1段目と同じ幅で左詰め） */
  .stat-row-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .stat-row-4 + .stat-row-4 { margin-top: .8rem; }
  @media (max-width: 640px) { .stat-row-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .stat-tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: .7rem .9rem; }
  .stat-label { font-size: .72rem; color: var(--text-muted); margin-bottom: .3rem; }
  .stat-value { font-weight: 600; }
  .stat-value-num { font-weight: 600; font-size: 1.3rem; }
  .stat-value-text { font-weight: 600; font-size: .95rem; }
  .stat-sub { font-size: .74rem; color: var(--text-muted); font-weight: 400; }

  /* status chip: color lives on the dot, never on the text */
  .status-chip { display: inline-flex; align-items: center; gap: .4rem; font-size: .8rem; font-weight: 600; color: var(--text-primary); white-space: nowrap; }
  .status-chip .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex: none; }
  .status-good .dot { background: var(--status-good); }
  .status-warning .dot { background: var(--status-warning); }
  .status-serious .dot { background: var(--status-serious); }
  .status-critical .dot { background: var(--status-critical); }
  .status-neutral .dot { background: var(--status-neutral); }

  .pill { display: inline-block; font-size: .75rem; color: var(--text-secondary); border: 1px solid var(--border); border-radius: 999px; padding: .1rem .6rem; }
  .chip { display: inline-block; font-size: .75rem; color: var(--text-secondary); background: var(--chip-surface); border: 1px solid var(--border); border-radius: 999px; padding: .15rem .6rem; margin: .1rem .3rem .1rem 0; }
  .chip-muted { color: var(--text-muted); font-style: italic; }
  .tag { display: inline-block; font-size: .72rem; color: var(--text-muted); background: var(--chip-surface); border: 1px solid var(--border); border-radius: 4px; padding: .05rem .4rem; margin-right: .3rem; }
  .chip-row { margin: .5rem 0; }

  /* 今週の要点 */
  ul.highlights { margin: 0; padding-left: 1.2rem; }
  ul.highlights li { padding: .3rem 0; }

  /* SEO順位・AIO引用の表、Appendixのキーワード別詳細カード */
  table.market-table { margin-bottom: .6rem; }
  table.market-table td.muted, table.market-table .muted { color: var(--text-muted); }
  table.pages-table th.num { text-align: right; }
  .rank-value { font-weight: 600; font-size: 1.3rem; }
  .cell-note { display: block; font-size: .72rem; font-weight: 400; color: var(--text-muted); }
  td.num .cell-note { display: inline; margin-left: .3rem; }
  ul.table-notes { margin: .4rem 0 0; padding-left: 1.1rem; font-size: .74rem; color: var(--text-muted); line-height: 1.6; }
  .reference-note { font-size: .74rem; color: var(--text-muted); border-top: 1px solid var(--gridline); margin-top: .7rem; padding-top: .5rem; }
  /* 2列固定 */
  .card-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
  @media (max-width: 760px) { .card-grid { grid-template-columns: 1fr; } }
  .query-card { border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.2rem; background: var(--market-surface); }
  .query-card .card-title { font-size: 1rem; margin: 0; color: var(--text-primary); }
  .query-card .card-judgment { margin-top: .3rem; }
  .domains { margin: .6rem 0; }
  .competitor-note { font-size: .8rem; color: var(--text-secondary); margin: .4rem 0; display: flex; gap: .5rem; align-items: baseline; }
  .match-condition { font-size: .74rem; color: var(--text-muted); margin-top: .5rem; }
  .variants { margin: .6rem 0; }
  .variant-list { list-style: none; padding: 0; margin: .3rem 0 0; font-size: .78rem; color: var(--text-secondary); max-height: 220px; overflow-y: auto; }
  .variant-list li { padding: .1rem 0; }
  .verdict { border-top: 1px solid var(--gridline); margin-top: .8rem; padding-top: .7rem; }
  .judgment-line { font-size: .85rem; margin-top: .4rem; color: var(--text-secondary); }

  /* generic finding list (Appendix) */
  ul.findings, ol.findings { list-style: none; padding-left: 0; }
  li.finding { border: 1px solid var(--border); border-radius: 8px; padding: .7rem .9rem; margin-bottom: .6rem; background: var(--surface-1); }
  li.priority-action { border-left: 3px solid var(--text-primary); }
  .action-head { display: flex; align-items: center; gap: .5rem; margin-bottom: .4rem; }
  .action-index { display: inline-flex; align-items: center; justify-content: center; width: 1.4rem; height: 1.4rem; border-radius: 50%; background: var(--text-primary); color: var(--surface-1); font-size: .78rem; font-weight: 700; flex: none; }
  .message { font-weight: 600; margin-top: .3rem; }
  .judgment { font-size: .85rem; color: var(--text-secondary); margin-top: .35rem; }
  .empty { color: var(--text-muted); font-style: italic; }
  .note { color: var(--text-secondary); font-size: .85rem; }
  .inquiry-status-missing { color: var(--status-critical); }

  /* tables */
  table.pages-table { border-collapse: collapse; width: 100%; font-size: .83rem; }
  table.pages-table th, table.pages-table td { border-bottom: 1px solid var(--gridline); padding: .5rem .6rem; text-align: left; vertical-align: top; }
  table.pages-table th { color: var(--text-muted); font-weight: 600; font-size: .74rem; text-transform: uppercase; letter-spacing: .02em; }
  table.pages-table td.num { text-align: right; font-variant-numeric: tabular-nums; }

  .excluded-list { font-size: .74rem; color: var(--text-muted); margin-top: .8rem; }
  .excluded-list ul { padding-left: 1.2rem; margin: .3rem 0 0; }

  /* Appendix */
  details.appendix { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.4rem; margin-top: 1rem; }
  details.appendix > summary { cursor: pointer; font-weight: 600; }

  /* Data Studio リンクボタン */
  /* Appendixは最下部。Data Studioリンクとの間隔は25px（appendix側のmargin-topは0にして相殺させない） */
  .datastudio-link { text-align: center; padding: 1.6rem 1.4rem; margin-bottom: 25px; }
  .datastudio-link + details.appendix { margin-top: 0; }
  .datastudio-button { display: inline-block; padding: .9rem 1.4rem; background: var(--text-primary); color: var(--surface-1); font-size: 1.05rem; font-weight: 600; text-decoration: none; border-radius: 6px; letter-spacing: .02em; }
</style>
"""


def render_html(vm: dict) -> str:
    if vm.get("layout") == "content":
        return render_html_content(vm)
    header = vm["header"]
    periods = header["periods"]
    sec = periods["sections"]
    notes = periods["notes"]
    # 現行方式は、①（6カードのサマリー）と「アクセスの詳細」を別のセクションに分ける
    v2_access = vm["site_usage"].get("scheme") == "v2"
    access_detail_html = ""
    if v2_access:
        access_detail_html = f"""
<section>
  <h2>アクセスの詳細{escape(sec['site_usage'])}</h2>
  {_site_usage_v2_html(vm["site_usage"])}{_notes_html(notes["site_usage"]) if notes["site_usage"] else ""}
</section>
"""

    body = f"""
<div class="report-root">
<h1>TCD週次SEO/AIOレポート（{escape(periods['title'])}）</h1>
<p class="note">{escape(periods['header_note'])} ／ データ品質: {escape(header['data_quality_label'])} ／ 継続週数: {escape(str(header.get('weeks_available', '-')))}週</p>

<section>
  <h2>① サイト全体のアクセス状況{escape(sec['site_usage'])}</h2>
  {_site_usage_html(vm["site_usage"])}{"" if v2_access else (_notes_html(notes["site_usage"]) if notes["site_usage"] else "")}
</section>
{access_detail_html}

<section>
  <h2>② 問い合わせ実績{escape(sec['inquiries'])}</h2>
  {_inquiries_html(vm["inquiries"])}
</section>

<section class="executive-summary">
  <h2>③ 今週の要点</h2>
  {_highlights_html(vm["executive_summary"])}
</section>

<section>
  <h2>④ SEO検索順位{escape(sec['seo'])}</h2>
  {_seo_table_html(vm["priority_markets"])}{_notes_html(notes["seo"]) if notes["seo"] else ""}
</section>

<section>
  <h2>⑤ AIO引用状況{escape(sec['aio'])}</h2>
  {_aio_table_html(vm["priority_markets"])}
</section>

<section class="datastudio-link">
  <a class="datastudio-button" href="https://datastudio.google.com/reporting/735a0062-1dd7-4e9b-9122-e6c308ad11f8/page/p_idoislic9d" target="_blank" rel="noopener">Data Studio で確認する →</a>
</section>

{_appendix_html(vm["appendix"], vm["priority_markets"], header)}
</div>
"""

    return (
        f"<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>TCD週次SEO/AIOレポート {escape(periods['title'].replace(' ', ''))}</title>"
        f"{_STYLE}</head><body>{body}</body></html>"
    )


# ══ SEO/AIO・コンテンツ評価中心の構成（2026-11-23 再設計、vm["layout"] == "content"） ══

def _p1(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def _pct1(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _page_name_html(p: dict) -> str:
    return f'{escape(p["short_title"])}<span class="cell-note">{escape(p["path"])}</span>'


def _points_html(points: list[str]) -> str:
    return '<ul class="fact-list">' + "".join(f"<li>{escape(x)}</li>" for x in points) + "</ul>" if points else "—"


def _eval_card_html(p: dict, with_points: bool) -> str:
    rows = [("事実（前週→今週）", escape(p["facts"])), ("評価", escape(p["evaluation"]))]
    if with_points and p["points"]:
        rows.append(("確認すべき論点（未確認）", _points_html(p["points"])))
    rows.append(("次の対応", escape(p["next_action"])))
    if p["notes"]:
        rows.append(("関連する施策", escape(p["notes"][0])))
    body = "".join(f'<div class="kv"><span class="k">{escape(k)}</span><span class="v">{v}</span></div>' for k, v in rows)
    return (f'<div class="query-card eval-card"><div class="card-head">{_status_chip(p["status"], p["state"])}'
            f'<span class="tag">{escape(p["intent_label"])}</span></div>'
            f'<h4 class="card-title">{escape(p["short_title"])}</h4><div class="match-condition">{escape(p["path"])}</div>{body}</div>')


def _lead_html(c: dict) -> str:
    """④ 今週のSEO/AIO・コンテンツ評価。冒頭①〜③の数値・事実は繰り返さず、評価・改善判断・次の対応だけを置く。"""
    lead = c["lead"]
    html = ['<h3>今週の評価</h3><ul class="lead-eval">'
            + "".join(f'<li><span class="tag">{escape(e["kind"])}</span>{escape(e["text"])}</li>' for e in lead["evaluation"])
            + "</ul>"]
    html.append('<h3>次の対応</h3><ol class="lead-actions">'
                + "".join(f"<li>{escape(a)}</li>" for a in lead["actions"]) + "</ol>"
                if lead["actions"] else '<h3>次の対応</h3><p class="empty">該当なし</p>')
    html.append(_notes_html(["数値と観測事実は①〜③。順位の変化は前週比（1週分）で、施策の効果判定ではない。AIOの変化は各回1回ずつの観測の比較。"
                             "根拠の詳細は⑤〜⑦、全ページの評価と区分の基準はAppendix。"]))
    return "".join(html)


# ── 冒頭②: AIO引用状況（コンパクトな一覧） ─────────────────────────────

_AIO_LOST_STYLE = "color:#b42318;font-weight:700"


def _aio_value_html(label: str) -> str:
    """あり＝強調、なし＝注意、判定不可・対象外・取得失敗は「なし」と区別して無彩色で示す。"""
    if label == "あり":
        return f'<strong style="{_AIO_CITED_STYLE}">あり</strong>'
    if label == "なし":
        return _status_chip("なし", "warning")
    if label.startswith("なし（"):
        return _status_chip(label, "neutral")
    return f'<span class="muted">{escape(label)}</span>'


def _aio_change_summary(ch: dict) -> tuple[str, bool]:
    names = {"generated": "AIO生成", "cited": "引用", "recommended": "推薦"}
    parts, lost = [], False
    for item in ("cited", "recommended", "generated"):
        v = ch["items"][item]
        if v["direction"]:
            text = f"{names[item]} {v['change']}"
            if v["direction"] == "lost" and item in ("cited", "recommended"):
                lost = True
                parts.append(f'<strong style="{_AIO_LOST_STYLE}">{escape(text)}</strong>')
            else:
                parts.append(escape(text))
    if parts:
        return "<br>".join(parts), lost
    if not ch["previous_available"]:
        return '<span class="muted">前回比較不可</span>', False
    unknown = [names[k] for k in ("cited", "recommended") if ch["items"][k]["change"] == "前回比較不可"]
    return "変化なし" + (f'<span class="cell-note">{escape("・".join(unknown))}は前回比較不可</span>' if unknown else ""), False


def _aio_top_html(c: dict) -> str:
    rows = []
    for ch in c["aio"]["changes"]:
        change, lost = _aio_change_summary(ch)
        rows.append([f"<strong>{escape(ch['market_label'])}</strong>",
                     escape(ch["items"]["generated"]["cur"]), _aio_value_html(ch["items"]["cited"]["cur"]),
                     _aio_value_html(ch["items"]["recommended"]["cur"]), change])
    obs = c["aio"]["changes"][0] if c["aio"]["changes"] else None
    note = (f"前回 {obs['previous_observed']} → 今回 {obs['current_observed']} の観測（各キーワード1回）の比較。"
            if obs else "")
    return (_table_html(["キーワード", "AIO生成", "TCD引用", "TCD推薦", "前回からの変化"], rows, num_from=99, extra_class="content-table")
            + _notes_html([note + "「判定不可」「対象外」は「なし」とは別（判定できない・推薦の判定対象ではない）。"
                           "前回比較の詳細・引用ドメイン・競合・日次観測の補足は⑤。"]))


# ── 冒頭③: SEO検索順位（コンパクトな一覧） ─────────────────────────────

def _seo_top_html(c: dict, seo_notes: list[str]) -> str:
    rows = []
    for r in c["keyword_pages"]:
        if r["tier"] == "reference":
            continue
        shown = r["position"] is not None and r["impressions"]
        pos = f'<span class="rank-value">{r["position"]:.1f}</span>' if shown else '— <span class="cell-note">表示データなし</span>'
        move = r.get("move") if shown else None
        delta = r.get("delta_label") or "—"
        if move == "順位上昇":
            chip = _status_chip(f"▲ {delta}", "good")
        elif move == "順位低下":
            chip = _status_chip(f"▼ {delta}", "critical")
        else:
            chip = _status_chip(delta, "neutral")
        related = f'<span class="cell-note">{escape(RELATED_QUERIES_NOTE)}</span>' if r.get("includes_related_queries") else ""
        rows.append([f"<strong>{escape(r['market_label'])}</strong>{related}", f'<span class="muted">{escape(r.get("volume_label") or "-")}</span>',
                     pos, chip, str(r["impressions"]), str(r["clicks"])])
    return (_table_html(["キーワード", "月間検索Vol.（参考）", "平均順位", "前週比", "Imp", "Click"], rows, num_from=2, extra_class="content-table")
            + _notes_html(["▲順位上昇／▼順位低下＝前週から2.0位以上の変化（1週分）。それ未満と参考値は無彩色。"
                           "ページ別の分析と改善候補は⑥。", *seo_notes]))


def _aio_changes_html(aio: dict) -> str:
    """⑤ 前回比較の補足。引用・推薦の変化そのものは②の一覧にあるため繰り返さず、日次観測の結果と、
    比較できなかった項目の理由（前回・今回の値）だけを置く。"""
    items = [*(aio.get("change_notes") or []), *(aio.get("comparison_supplements") or [])]
    if not items:
        return ""
    return "<h3>前回比較の補足（日次観測など）</h3>" + _notes_html(items)


def _aio_content_html(c: dict) -> str:
    aio = c["aio"]
    rows = []
    for r in aio["rows"]:
        top = r["top_page"]
        rows.append([escape(r["market_label"]),
                     escape(r["competitor_recommendation_label"]),
                     escape(r["citation_count_label"]),
                     (f"{escape(top['name'])}<span class='cell-note'>{_p1(top['position'])}位・表示{top['impressions']}</span>" if top else "—")])
    html = [_aio_changes_html(aio), "<h3>今回の観測（引用ドメイン・競合）</h3>",
            _table_html(["キーワード", "競合の推薦", "引用ドメイン", "TCDで最も表示されたページ"],
                        rows, num_from=99, extra_class="content-table")]
    cards = []
    for r in aio["rows"]:
        facts = "".join(f"<li>{escape(x)}</li>" for x in r["facts"])
        pts = "".join(f"<li>{escape(x)}</li>" for x in r["points"])
        cards.append(f'<div class="query-card"><h4 class="card-title">{escape(r["market_label"])}</h4>'
                     f'<div class="sub-head">観測事実</div><ul class="fact-list">{facts}</ul>'
                     + (f'<div class="sub-head">確認する論点（未検証）</div><ul class="fact-list guess">{pts}</ul>' if pts else "")
                     + f'<div class="judgment-line">次の対応: {escape(r["next_action"])}</div></div>')
    html.append('<div class="card-grid">' + "".join(cards) + "</div>")
    notes = []
    if aio["cross_domains"]:
        notes.append("複数キーワードで引用されたドメイン: " + "、".join(f"{d}（{'・'.join(qs)}）" for d, qs in aio["cross_domains"].items()))
    html.append(_notes_html(notes + aio["notes"]))
    return "".join(html)


def _kw_page_html(p: dict) -> str:
    prev = (f"前週{p['prev_impressions']}回・{_p1(p['prev_position'])}位" if p.get("prev_impressions") is not None else "前週表示なし")
    return f"<div>{escape(p['name'])}<span class='cell-note'>今週{p['impressions']}回・{_p1(p['position'])}位（{prev}）</span></div>"


def _seo_content_html(c: dict, seo_notes: list[str]) -> str:
    krows, checks = [], []
    for r in c["keyword_pages"]:
        if r["tier"] == "reference":
            continue
        pages = "".join(_kw_page_html(p) for p in r["pages"][:3]) or "—"
        krows.append([escape(r["market_label"]), escape(r["intent_label"]), pages, escape(r["page_evaluation"] or "—")])
        if r["movement"]:
            checks.append(f'<div class="query-card"><h4 class="card-title">「{escape(r["market_label"])}」の順位変動（前週比・1週）</h4>'
                          '<div class="sub-head">このキーワードに一致したクエリでの、ページ別の表示（事実）</div>'
                          + _points_html(r["movement"])
                          + '<div class="judgment-line">次の対応: ページ×クエリで、どのクエリ・どのページの順位が変わったかを確認する。'
                            '1週の変化のため、これだけで改修は決めない</div></div>')
        if r["investigate"]:
            checks.append(f'<div class="query-card"><h4 class="card-title">調査対象: 「{escape(r["market_label"])}」</h4>'
                          f'<div class="sub-head">事実</div><ul class="fact-list"><li>{escape(r["page_evaluation"])}</li></ul>'
                          f'<div class="judgment-line">次の対応: {escape(r["investigate"])}</div></div>')
    html = ["<h3>重点キーワードで表示されたページ</h3>",
            _table_html(["キーワード", "検索意図", "表示されたページ（上位3。今週・前週）", "評価"], krows, num_from=99, extra_class="content-table")]
    if checks:
        html.append('<div class="card-grid">' + "".join(checks) + "</div>")
    lrows = [[_status_chip(p["status"], p["state"]), _page_name_html(p), f"{_p1(p['prev_position'])}→{_p1(p['position'])}",
              f"{p['prev_impressions']:,}→{p['impressions']:,}", f"{p['prev_clicks']}→{p['clicks']}",
              f"{_pct1(p['prev_ctr'])}→{_pct1(p['ctr'])}", escape("、".join(p["top_queries"]) or "—"),
              _eval_cell_html(p)] for p in c["regional_lps"]]
    if lrows:
        html += ["<h3>地域LPの検索実績（全クエリの合計）</h3>",
                 _table_html(["区分", "ページ", "順位（前週→今週）", "表示", "クリック", "CTR", "主要クエリ（上位3）", "評価・確認すべき論点・次の対応"], lrows, num_from=99, extra_class="content-table"),
                 _notes_html(["上の表の「重点キーワード」の値は、そのキーワードに一致するクエリだけの集計。この表は地域LPに表示された全クエリの合計で、両者は一致しない。"])]
    lead = c["lead"]
    html.append("<h3>改善候補（確認が必要なコンテンツ）</h3>")
    html.append('<div class="card-grid">' + "".join(_eval_card_html(p, True) for p in lead["candidates"]) + "</div>"
                if lead["candidates"] else '<p class="empty">該当なし</p>')
    notes = []
    if lead["thin_candidates"]:
        names = "、".join(f"「{p['short_title']}」" for p in lead["thin_candidates"])
        notes.append(f"評価材料不足（表示が週50回未満）のサービス・地域・比較ページ{len(lead['thin_candidates'])}件も改善候補に含める: "
                     f"{names}。順位・クリックの変化は評価していない。")
    notes += ["地域LPは上の「地域LPの検索実績」に記載。全ページの評価（順位上昇・順位安定を含む）と区分の基準はAppendixの「ページ別評価（全件）」。",
              *seo_notes]
    html.append(_notes_html(notes))
    return "".join(html)


def _eval_cell_html(p: dict) -> str:
    ev = escape(p["evaluation"])
    if p["points"]:
        ev += "<span class='cell-note'>論点: " + escape("／".join(p["points"])) + "</span>"
    return ev + f"<span class='cell-note'>次: {escape(p['next_action'])}</span>"


def _metric_table_html(lines: list[dict]) -> str:
    ref = "<span class='cell-note'>参考</span>"
    rows = "".join(f"<tr><td>{escape(x['label'])}{'' if x['judged'] else ref}</td>"
                   f"<td>{escape(x['target'])}</td><td>{escape(x['control'])}</td><td>{escape(x['diff'])}</td></tr>" for x in lines)
    return ('<table class="mini-table"><thead><tr><th>指標</th><th>対象ページ（実装前→直近）</th><th>比較対照</th>'
            f'<th>比較対照との差</th></tr></thead><tbody>{rows}</tbody></table>')


def _initiatives_content_html(c: dict) -> str:
    ini = c["initiatives"]
    cnt = ini["counts"]
    cards = []
    for r in ini["rows"]:
        ids = "".join(f"<li>{escape(i['name'])}<span class='cell-note'>{escape(i['id'])}・{escape(i['verdict'] or '—')}</span></li>"
                      for i in r["items"])
        title = escape("／".join(r["names"]))
        body = [f'<div class="card-head">{_status_chip(r["class"], r["state"])}<span class="tag">{escape(r["verdict_short"])}</span></div>',
                f'<h4 class="card-title">{title}</h4>',
                f'<div class="kv"><span class="k">実装日</span><span class="v">{escape(r["implemented_at"] or "—")}'
                + (f"（対象{r['url_count']}ページ）" if r["url_count"] else "") + "</span></div>",
                f'<div class="kv"><span class="k">改修内容・仮説</span><span class="v">{escape(r["hypothesis"] or "—")}</span></div>']
        if r["merged"]:
            body.append(f'<div class="kv"><span class="k">施策（個別）</span><span class="v"><ul class="fact-list">{ids}</ul>'
                        "<span class='cell-note'>対象ページ・実装日・判定・数値がすべて同じため表示上まとめた</span></span></div>")
        if r["window"]:
            body.append(f'<div class="kv"><span class="k">比較期間</span><span class="v">{escape(r["window"]["label"])}'
                        f'<span class="cell-note">{escape(r["window"]["note"])}</span></span></div>')
        if r["metric_lines"]:
            body.append(_metric_table_html(r["metric_lines"]))
        elif r["reason"]:
            body.append(f'<div class="kv"><span class="k">評価しない理由</span><span class="v">{escape(r["reason"])}</span></div>')
        if r["overlaps"]:
            body.append(f'<div class="kv"><span class="k">重なる施策</span><span class="v">{escape("・".join(r["overlaps"]))}</span></div>')
        body.append(f'<div class="judgment-line">次に確認すること: {escape(r["next_action"] or "—")}</div>')
        cards.append(f'<div class="query-card eval-card initiative-card">{"".join(body)}</div>')
    return (f'<p class="lede-line">施策{ini["item_count"]}件（表示{len(ini["rows"])}行）: 評価可能 {cnt["評価可能"]}・要調査 {cnt["要調査"]}・評価保留 {cnt["評価保留"]}</p>'
            + '<div class="initiative-list">' + "".join(cards) + "</div>" + _notes_html(ini["notes"]))


def _inquiry_dashboard_html(c: dict) -> str:
    d = c["inquiry_dashboard"]
    tiles = []
    for t in d["tiles"]:
        cur = "未集計" if t["current"] is None else f"{t['current']:,}"
        cum = escape(inquiry_cumulative_label(d["cumulative"], t["cumulative_4w"]))
        cls = " stat-tile-primary" if t["primary"] else ""
        tiles.append(f'<div class="stat-tile{cls}"><div class="stat-label">{escape(t["label"])}</div>'
                     f'<div class="stat-value-num">{cur}</div><div class="stat-sub">直近4週 {cum}</div></div>')
    return (f'<div class="stat-row stat-row-5">{"".join(tiles)}</div>'
            + _notes_html([d["status_line"] + "。案件相談は週ごとの変動が大きいため、評価には累計を使う。",
                           *(f"集計の警告: {w}" for w in d["warnings"])]))


def _appendix_pages_all_html(c: dict) -> str:
    rows = [[_status_chip(p["status"], p["state"]), _page_name_html(p), escape(p["intent_label"]),
             escape("、".join(p["top_queries"]) or "—"), f"{_p1(p['prev_position'])}→{_p1(p['position'])}",
             f"{p['prev_impressions']:,}→{p['impressions']:,}", f"{p['prev_clicks']}→{p['clicks']}", _pct1(p["ctr"]), _eval_cell_html(p)]
            for p in c["pages"]]
    return (_table_html(["区分", "ページ", "検索意図", "主要クエリ（上位3）", "順位（前週→今週）", "表示（前週→今週）", "クリック", "CTR",
                         "評価・確認すべき論点・次の対応"], rows, num_from=99, extra_class="content-table")
            + _notes_html([c["status_rules"], "順位・表示・CTR・クリックは判断材料で、増減だけで良否を決めない。"]))


_CONTENT_STYLE = """
  .stat-row-5 { grid-template-columns: repeat(5, minmax(0, 1fr)); }
  @media (max-width: 640px) { .stat-row-5 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .stat-tile-primary { border: 2px solid var(--text-primary); }
  .eval-card .card-head { display: flex; gap: .5rem; align-items: center; margin-bottom: .3rem; }
  .eval-card .kv { display: grid; grid-template-columns: 7.5em 1fr; gap: .5rem; font-size: .8rem; padding: .25rem 0; border-top: 1px solid var(--gridline); }
  .eval-card .k { color: var(--text-muted); }
  .sub-head { font-size: .74rem; color: var(--text-muted); font-weight: 600; margin-top: .6rem; }
  ul.fact-list { margin: .2rem 0 .4rem; padding-left: 1.1rem; font-size: .8rem; }
  ul.fact-list.guess { color: var(--text-secondary); font-style: italic; }
  .lede-line { font-weight: 600; margin: 0 0 .6rem; }
  table.content-table th { white-space: nowrap; }
  table.content-table td.nowrap { white-space: nowrap; }
  .initiative-list { display: grid; gap: .8rem; }
  table.mini-table { width: 100%; border-collapse: collapse; font-size: .78rem; margin: .4rem 0; }
  table.mini-table th, table.mini-table td { text-align: left; padding: .25rem .4rem; border-top: 1px solid var(--gridline); vertical-align: top; }
  table.mini-table th { color: var(--text-muted); font-weight: 600; white-space: nowrap; }
  .eval-card .v ul.fact-list { margin: 0; }
  ul.lead-eval { list-style: none; padding-left: 0; }
  ul.lead-eval li { margin: .3rem 0; }
  ul.lead-eval .tag { margin-right: .5rem; }
  ol.lead-actions li { margin: .3rem 0; }
  .kpi-line { font-size: .8rem; color: var(--text-secondary); border-top: 1px solid var(--gridline); padding-top: .6rem; margin-top: .8rem; }
"""


def render_html_content(vm: dict) -> str:
    header = vm["header"]
    periods = header["periods"]
    sec = periods["sections"]
    notes = periods["notes"]
    c = vm["content"]
    appendix = vm["appendix"]
    initiatives_title, initiatives_html = _initiatives_html(appendix)
    seo_notes = [n.replace("改善・悪化の判定", "順位上昇・低下の判定") for n in vm["priority_markets"]["seo_notes"] + notes["seo"]]
    body = f"""
<div class="report-root">
<h1>TCD週次SEO/AIOレポート（{escape(periods['title'])}）</h1>
<p class="note">{escape(periods['header_note'])} ／ データ品質: {escape(header['data_quality_label'])} ／ 継続週数: {escape(str(header.get('weeks_available', '-')))}週</p>

<section>
  <h2>① サイト全体のアクセス状況{escape(sec['site_usage'])}</h2>
  {_summary_tiles_html(vm["site_usage"])}
  {_notes_html([f"カードの数値: GA4 {vm['site_usage']['metric_label']}（人）。前週比は現行の集計条件どうしの比較。流入元・推移などの詳細は⑨。",
                *(f"異常値（原因未確認）: {a}" for a in vm["site_usage"]["anomaly_notes"])])}
</section>

<section>
  <h2>② AIO引用状況{escape(sec['aio'])}</h2>
  {_aio_top_html(c)}
</section>

<section>
  <h2>③ SEO検索順位{escape(sec['seo'])}</h2>
  {_seo_top_html(c, seo_notes)}
</section>

<section class="executive-summary">
  <h2>④ 今週のSEO/AIO・コンテンツ評価</h2>
  {_lead_html(c)}
</section>

<section>
  <h2>⑤ AIO詳細・競合比較{escape(sec['aio'])}</h2>
  {_aio_content_html(c)}
</section>

<section>
  <h2>⑥ SEO・コンテンツ改善候補{escape(sec['seo'])}</h2>
  {_seo_content_html(c, [])}
</section>

<section>
  <h2>⑦ 実施施策と効果検証</h2>
  {_initiatives_content_html(c)}
</section>

<section>
  <h2>⑧ 問い合わせ実績{escape(sec['inquiries'])}</h2>
  {_inquiry_dashboard_html(c)}
</section>

<section>
  <h2>⑨ アクセスの詳細{escape(sec['site_usage'])}</h2>
  {_site_usage_v2_html(vm["site_usage"])}{_notes_html(notes["site_usage"]) if notes["site_usage"] else ""}
</section>

<section class="datastudio-link">
  <a class="datastudio-button" href="https://datastudio.google.com/reporting/735a0062-1dd7-4e9b-9122-e6c308ad11f8/page/p_idoislic9d" target="_blank" rel="noopener">Data Studio で確認する →</a>
</section>

<details class="appendix">
  <summary>Appendix　※クリックして表示</summary>
  <h3>キーワード別詳細（クエリ内訳・AIO引用元・判定）</h3>
  {_keyword_details_html(vm["priority_markets"])}
  <h3>ページ別評価（全件・評価材料不足を含む）</h3>
  {_appendix_pages_all_html(c)}
  <h3>{escape(initiatives_title)}</h3>
  {initiatives_html}
  <h3>データ品質</h3>
  {_appendix_data_quality_html(appendix["data_quality"])}
  <h3>上位ランディングページ（/branding/、定点観測対象外）</h3>
  {_appendix_landing_pages_html(appendix["landing_pages"])}
</details>
</div>
"""
    return (
        f"<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>TCD週次SEO/AIOレポート {escape(periods['title'].replace(' ', ''))}</title>"
        f"{_STYLE.replace('</style>', _CONTENT_STYLE + '</style>')}</head><body>{body}</body></html>"
    )
