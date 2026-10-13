"""View ModelからHTMLを生成する。分析ロジックは追加しない（整形のみ）。

render_md.pyと同じView Modelを参照し、同じFinding集合を表示する
（Markdown/HTML間で内容差異が生じないようにするため）。

レポート構成（4セクション + Appendix。2026-10-13改善指示書）:
① サイト全体のアクセス状況 → ② 今週の要点 → ③ SEO検索順位 → ④ AIO引用状況 → Appendix。
判断に必要な情報を本文に残し、根拠の全件データはAppendix（<details>）へ移す。
"""
from __future__ import annotations

import re
from html import escape

from src_v2.report.view_model import RELATED_QUERIES_NOTE, judgment_label

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


# ── ② 今週の要点 ────────────────────────────────────────────────

# AIOでのTCD引用は最重要指標のため、本文中の該当文言を赤字・太字で強調する（2026-09-30ユーザー指示）。
# メール（Gmail）でも確実に効くよう、CSS変数ではなくインラインstyleで指定する。
_AIO_CITED_PHRASES = ("TCD AIO引用あり", "AIOでTCD引用あり", "AIO引用あり", "TCD引用あり")
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

def _site_usage_html(site_usage: dict) -> str:
    segments = site_usage.get("segments") or []
    if not segments:
        return "<p class='empty'>GA4データなし</p>"

    rows_html = []
    for row in site_usage.get("rows") or [segments]:
        rows_html.append(f'<div class="stat-row stat-row-4">{"".join(_site_tile_html(seg) for seg in row)}</div>')
    return "".join(rows_html)


def _site_tile_html(seg: dict) -> str:
    if seg.get("error"):
        return f"""
        <div class="stat-tile">
          <div class="stat-label">{escape(seg['label'])}</div>
          <div class="stat-value-text">取得失敗</div>
        </div>"""

    delta_pct = seg.get("delta_pct")
    delta_html = ""
    if delta_pct is not None:
        state = "good" if delta_pct >= 0 else "critical"
        delta_html = f'<div class="stat-sub">{_status_chip(f"{delta_pct:+.1f}%", state)}（前週比）</div>'
    return f"""
        <div class="stat-tile">
          <div class="stat-label">{escape(seg['label'])}</div>
          <div class="stat-value-num">{_fmt_num(seg.get("current"))}</div>
          {delta_html}
        </div>"""


# ── ③ SEO検索順位（重点4キーワードを1つの表で表示） ──────────────────

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
    pos_html = f"{row['position']:.1f}" if shown else '— <span class="cell-note">GSCで表示データなし</span>'
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


# ── ④ AIO引用状況（同じ4キーワード。SEO順位とは別の表にする） ──────────

def _aio_table_html(pm: dict) -> str:
    if not pm["table"]:
        return "<p class='empty'>対象キーワードなし</p>"
    return f"""
    <table class="pages-table market-table">
      <thead>
        <tr><th>キーワード</th><th>AIO生成</th><th>TCD引用</th><th>主な引用元</th></tr>
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
    sources_class = ' class="muted"' if row["citation_unavailable"] else ""
    return f"""
    <tr>
      <td><strong>{escape(row['market_label'])}</strong></td>
      <td>{escape(row['aio_generated_label'])}</td>
      <td>{tcd_html}</td>
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
        domain_chips = "".join(f'<span class="chip">{escape(d)}</span>' for d in cited_domains) or '<span class="chip chip-muted">引用元なし</span>'

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
    unknown_initiatives_html = (
        "<ul class=\"findings\">" + "".join(_initiative_html(f, show_confidence=True) for f in appendix["unknown_initiatives"]) + "</ul>"
        if appendix["unknown_initiatives"] else "<p class='empty'>なし</p>"
    )
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

      <h3>実装日未登録の施策</h3>
      {unknown_initiatives_html}

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
  table.market-table td.muted { color: var(--text-muted); }
  table.pages-table th.num { text-align: right; }
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
    header = vm["header"]
    period = header["period"]

    body = f"""
<div class="report-root">
<h1>TCD週次SEO/AIOレポート（{escape(period['cur_start'])} 〜 {escape(period['cur_end'])}）</h1>
<p class="note">対象期間: {escape(period['cur_start'])}〜{escape(period['cur_end'])}（データ確定基準: 実行日-3日オフセット） ／ データ品質: {escape(header['data_quality_label'])} ／ 継続週数: {escape(str(header.get('weeks_available', '-')))}週</p>

<section>
  <h2>① サイト全体のアクセス状況</h2>
  {_site_usage_html(vm["site_usage"])}
</section>

<section class="executive-summary">
  <h2>② 今週の要点</h2>
  {_highlights_html(vm["executive_summary"])}
</section>

<section>
  <h2>③ SEO検索順位</h2>
  {_seo_table_html(vm["priority_markets"])}
</section>

<section>
  <h2>④ AIO引用状況</h2>
  {_aio_table_html(vm["priority_markets"])}
</section>

<section class="datastudio-link">
  <a class="datastudio-button" href="https://datastudio.google.com/reporting/4d67c760-d3dc-4a1e-b2d2-47ce3b44f69a/page/p_m2bpi11y5d" target="_blank" rel="noopener">Data Studio で確認する →</a>
</section>

{_appendix_html(vm["appendix"], vm["priority_markets"], header)}
</div>
"""

    return (
        f"<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>TCD週次SEO/AIOレポート {escape(period['cur_start'])}〜{escape(period['cur_end'])}</title>"
        f"{_STYLE}</head><body>{body}</body></html>"
    )
