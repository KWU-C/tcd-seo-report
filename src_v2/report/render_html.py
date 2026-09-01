"""View ModelからHTMLを生成する。分析ロジックは追加しない（整形のみ）。

render_md.pyと同じView Modelを参照し、同じFinding集合を表示する
（Markdown/HTML間で内容差異が生じないようにするため）。

レポート構成（6セクション + Appendix。report.html改善指示書 3節）。
判断に必要な情報を本文に残し、根拠の全件データはAppendix（<details>）へ移す。
"""
from __future__ import annotations

import re
from html import escape

_SEVERITY_STATUS = {"critical": "critical", "warning": "warning", "info": "neutral"}
_REGION_LABEL = {"tokyo": "東京", "osaka": "大阪"}
_MARKET_LABEL = {"branding": "ブランディング", "inner_branding": "インナーブランディング"}


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
    judgment_label = f.get("judgment") or "-"
    return _status_chip(judgment_label, state)


def _judgment_confidence_html(f: dict) -> str:
    """Appendix用: 状態（=judgment）＋確度（=confidence）の2軸表示。"""
    return f'{_judgment_chip(f)} <span class="pill">確度: {escape(f["confidence"])}</span>'


def _fmt1(n) -> str:
    return f"{n:.1f}" if isinstance(n, (int, float)) else "-"


def _fmt_num(n) -> str:
    return "-" if n is None else escape(str(n))


# ── ① 今週の結論（Executive Summary） ──────────────────────────

_HEADING_RE = re.compile(r"^(●[^\n—-]*[^\n—\s-]|[A-F]\.[^\n—-]*[^\n—\s-])")


def _paragraph_html(paragraph: str) -> str:
    """「●見出し」（または旧形式のA〜F.見出し）ラベルのみ太字にする。見出し直後の
    区切り（改行や「 — 」）はそのまま保持する（改行なら見出しの太字化後も改行として残す）。"""
    m = _HEADING_RE.match(paragraph)
    if not m:
        return escape(paragraph)
    heading = m.group(1)
    rest = paragraph[len(heading):]
    return f"<strong>{escape(heading)}</strong>{escape(rest)}"


def _executive_summary_html(es: dict) -> str:
    narrative = es.get("narrative")
    if narrative:
        # 空行(\n\n)を段落境界とする。単一の\nは同じ段落内の改行として扱い、
        # 行ごとに区切り線が入る（下線が多すぎる）状態を避ける。
        paragraphs = [p for p in narrative.strip().split("\n\n") if p.strip()]
    else:
        paragraphs = es["blocks"]
    return "".join(f"<p>{_paragraph_html(block)}</p>" for block in paragraphs)


# ── ② サイトアクセス状況（GA4） ─────────────────────────────────────

def _site_usage_html(site_usage: dict) -> str:
    segments = site_usage.get("segments") or []
    if not segments:
        return "<p class='empty'>GA4データなし</p>"

    tiles = []
    for seg in segments:
        if seg.get("error"):
            tiles.append(f"""
        <div class="stat-tile">
          <div class="stat-label">{escape(seg['label'])}</div>
          <div class="stat-value-text">取得失敗</div>
        </div>""")
            continue

        cur = seg.get("current")
        delta_pct = seg.get("delta_pct")
        delta_html = ""
        if delta_pct is not None:
            state = "good" if delta_pct >= 0 else "critical"
            delta_html = f'<div class="stat-sub">{_status_chip(f"{delta_pct:+.1f}%", state)}（前期間比）</div>'
        tiles.append(f"""
        <div class="stat-tile">
          <div class="stat-label">{escape(seg['label'])}</div>
          <div class="stat-value-num">{_fmt_num(cur)}</div>
          {delta_html}
        </div>""")

    return f'<div class="stat-row">{"".join(tiles)}</div>'


# ── ③ 重点市場（4市場固定・一覧表 + 詳細カード） ──────────────────

def _market_table_html(table: list[dict]) -> str:
    if not table:
        return "<p class='empty'>対象市場なし</p>"

    rows = []
    for row in table:
        delta = row["position_delta"]
        tier = row["delta_tier"]
        if row["position"] is None:
            pos_html = _status_chip("-位", "neutral")
        elif delta is None or tier == "flat":
            pos_html = _status_chip(f"{row['position']:.1f}位", "neutral")
        else:
            arrow = "▲" if delta > 0 else "▼"
            state = "neutral" if tier == "minor" else ("good" if delta > 0 else "critical")
            pos_html = _status_chip(f"{arrow} {row['position']:.1f}位（{delta:+.1f}）", state)

        aio_state = {"引用あり": "good", "未引用": "warning", "AIO未生成": "neutral"}.get(row["aio_status"], "neutral")

        rows.append(f"""
        <tr>
          <td><strong>{escape(row['market_label'])}</strong></td>
          <td>{escape(row['seo_status'])}</td>
          <td>{pos_html}</td>
          <td class="num">{row['impressions']}</td>
          <td class="num">{row['clicks']}</td>
          <td>{_status_chip(row['aio_status'], aio_state)}</td>
          <td>{escape(row.get('judgment') or '-')}</td>
        </tr>""")

    return f"""
    <table class="pages-table market-table">
      <thead>
        <tr><th>市場</th><th>SEO状態</th><th>順位</th><th>Imp</th><th>Click</th><th>AIO</th><th>総合評価</th></tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


def _market_card_html(f: dict) -> str:
    evidence = f.get("evidence") or {}
    query = evidence.get("query", "")
    region_chip = _REGION_LABEL.get(evidence.get("region"), evidence.get("region") or "")
    market_chip = _MARKET_LABEL.get(evidence.get("market"), evidence.get("market") or "")
    tokens = evidence.get("match_tokens") or []
    cur = evidence.get("current") or {}
    prv = evidence.get("comparison") or {}
    variants = evidence.get("matched_query_variants") or []
    aio = evidence.get("aio") or {}

    tcd_cited = aio.get("tcd_cited")
    cited_domains = [d for d in (aio.get("cited_domains") or []) if d and d != "tcd.jp"]
    tcd_state = "good" if tcd_cited is True else ("warning" if tcd_cited is False else "neutral")
    tcd_label = "TCD引用あり" if tcd_cited is True else ("TCD未引用" if tcd_cited is False else "AIO評価対象外")

    competitor_notes = ""
    for cf in f.get("competitor_findings", []):
        direction_state = "warning" if "new" in cf["id"] and cf["severity"] == "warning" else "neutral"
        competitor_notes += f'<div class="competitor-note">{_status_chip("競合動向", direction_state)} {escape(cf["display_message"] or "-")}</div>'

    domain_chips = "".join(f'<span class="chip">{escape(d)}</span>' for d in cited_domains) or '<span class="chip chip-muted">引用ドメインなし</span>'

    if variants:
        variant_items = "".join(
            f'<li>「{escape(v["query"])}」 imp={v["impressions"]} / clicks={v["clicks"]}</li>' for v in variants
        )
        variants_html = f'<ul class="variant-list">{variant_items}</ul>'
    else:
        variants_html = "<p class='empty'>一致した実クエリなし</p>"

    return f"""
    <details class="query-card" open>
      <summary>
        <span class="tag">{escape(market_chip)}</span><span class="tag">{escape(region_chip)}</span>
        <strong>{escape(query)}</strong>
        {_judgment_chip(f)}
      </summary>

      <div class="match-condition">一致条件（AND・語順不問）: {" + ".join(escape(t) for t in tokens)}</div>

      <div class="stat-row">
        <div class="stat-tile">
          <div class="stat-label">順位</div>
          <div class="stat-value-lg">{_position_stat_lg_html(
              (cur or {}).get('position'), (prv or {}).get('position'), f.get('position_delta'), f.get('delta_tier', 'flat')
          )}</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">Impressions</div>
          <div class="stat-value-num">{cur.get('impressions', 0)}<span class="stat-sub"> (前週 {prv.get('impressions', 0)})</span></div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">Clicks</div>
          <div class="stat-value-num">{cur.get('clicks', 0)}<span class="stat-sub"> (前週 {prv.get('clicks', 0)})</span></div>
        </div>
      </div>

      <div class="chip-row">{_status_chip(tcd_label, tcd_state)}</div>

      <div class="domains">
        <div class="stat-label">AIO引用ドメイン（TCD以外）</div>
        <div class="chip-row">{domain_chips}</div>
      </div>

      {competitor_notes}

      <div class="variants">
        <div class="stat-label">一致した実クエリ内訳（全件）</div>
        {variants_html}
      </div>

      <div class="verdict">
        <div class="message">{escape(f['display_message'] or '-')}</div>
        <div class="judgment-line">推奨: {escape(f['recommendation'])}</div>
      </div>
    </details>"""


def _priority_markets_html(pm: dict) -> str:
    table_html = _market_table_html(pm["table"])
    cards_html = "".join(_market_card_html(c) for c in pm["cards"])
    return f"""{table_html}
    <div class="card-grid">{cards_html}</div>"""


# ── ④ カテゴリ・ページ動向 ────────────────────────────────────────

def _category_summary_html(summary: list[dict]) -> str:
    if not summary:
        return "<p class='empty'>対象ページなし</p>"

    rows = []
    for s in summary:
        cur, prv = s["current"], s["comparison"]
        click_delta = cur["clicks"] - prv["clicks"]
        delta_state = "good" if click_delta > 0 else ("critical" if click_delta < 0 else "neutral")
        rows.append(f"""
        <tr>
          <td><strong>{escape(s['label'])}</strong></td>
          <td>{prv['impressions']}→{cur['impressions']}</td>
          <td>{prv['clicks']}→{cur['clicks']} {_status_chip(f'{click_delta:+d}', delta_state)}</td>
        </tr>""")

    return f"""
    <table class="pages-table">
      <thead><tr><th>カテゴリ</th><th>Imp変化</th><th>Click変化</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


def _position_stat_lg_html(cur_pos, prv_pos, delta, tier: str) -> str:
    """③ 重点市場カードの「順位」タイル用。大きな現在値＋小さな前週比の添え書き
    （旧デザインの.stat-value-lgスタイルを踏襲）。tier=flatの場合は矢印・色を付けない
    （表示回数が少ない/変化が僅かな場合に順位差を強調しない）。
    """
    if cur_pos is None:
        return _status_chip("-位", "neutral")
    if prv_pos is None or delta is None:
        return _status_chip(f"{cur_pos:.1f}位", "neutral")
    if tier == "flat":
        chip = _status_chip(f"{cur_pos:.1f}位", "neutral")
    else:
        arrow = "▲" if delta > 0 else "▼"
        state = "neutral" if tier == "minor" else ("good" if delta > 0 else "critical")
        chip = _status_chip(f"{arrow} {cur_pos:.1f}位", state)
    sub = f'<span class="stat-sub"> (前週 {prv_pos:.1f}位、{delta:+.1f})</span>'
    return chip + sub


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


def _category_page_trends_html(cpt: dict, low_impressions_threshold: int) -> str:
    notable = cpt["notable"]
    return f"""
    <h3>カテゴリ別</h3>
    {_category_summary_html(cpt["category_summary"])}
    <h3>要確認ページ</h3>
    <p class="note">Impressions・順位差・クリック差が一定以上のページ、または重点市場の主対象URLのみ表示（※ Impressions {low_impressions_threshold}未満は参考値のため、それ以外はAppendix参照）。</p>
    <div class="trend-band">
      <h4>上昇</h4>
      {_pages_table_html(notable["improved"])}
    </div>
    <div class="trend-band">
      <h4>下落</h4>
      {_pages_table_html(notable["declined"])}
    </div>"""


# ── ⑤ 施策評価 ──────────────────────────────────────────────────

def _initiative_html(f: dict, show_confidence: bool = False) -> str:
    status_html = _judgment_confidence_html(f) if show_confidence else _judgment_chip(f)
    return f"""
    <li class="finding">
      {status_html}
      <div class="message">{escape(f['display_message'] or '-')}</div>
      <div class="judgment">推奨: {escape(f['recommendation'])}</div>
    </li>"""


def _initiatives_html(initiatives: dict) -> str:
    known = initiatives["known"]
    unknown_count = initiatives["unknown_count"]

    parts = []
    if unknown_count:
        parts.append(f"<p class='note'>施策評価: 実装日未登録の{unknown_count}施策があるため評価不能（詳細はAppendix）</p>")
    if known:
        parts.append("<ul class=\"findings\">" + "".join(_initiative_html(f) for f in known) + "</ul>")
    elif not unknown_count:
        parts.append("<p class='empty'>評価対象の施策なし</p>")

    return "".join(parts)


# ── ⑥ 今週やること（Priority Action、最大3件） ────────────────────

def _this_week_actions_html(actions: list[dict]) -> str:
    if not actions:
        return "<p class='empty'>今週追加で実施すべきアクションはなし（継続観測中心）。</p>"

    items = []
    for i, f in enumerate(actions, start=1):
        items.append(f"""
    <li class="finding priority-action">
      <div class="action-head"><span class="action-index">{i}</span><strong>{escape(f['action_target'])}</strong></div>
      {_judgment_chip(f)}
      <div class="message">{escape(f['display_message'] or '-')}</div>
      <div class="judgment">対応: {escape(f['recommendation'])}</div>
    </li>""")
    return '<ol class="findings priority-actions">' + "".join(items) + "</ol>"


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
      <li>データ品質: {escape(dq.get('status', '-'))}</li>
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


def _appendix_html(appendix: dict, header: dict) -> str:
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
  /* ④ 要確認ページ「上昇」「下落」の帯（--page-planeで区切り、本文セクション内でも視覚的に独立させる） */
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
  .stat-tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: .7rem .9rem; }
  .stat-label { font-size: .72rem; color: var(--text-muted); margin-bottom: .3rem; }
  .stat-value { font-weight: 600; }
  .stat-value-num { font-weight: 600; font-size: 1.3rem; }
  .stat-value-text { font-weight: 600; font-size: .95rem; }
  .stat-sub { font-size: .74rem; color: var(--text-muted); font-weight: 400; }
  /* SEO順位: Impressionsと同じ大きさで見せる（.status-chipは.8remで自己指定しているため個別に上書き） */
  .stat-value-lg .status-chip { font-size: 1.3rem; font-weight: 600; gap: .5rem; white-space: normal; align-items: flex-start; line-height: 1.3; }
  .stat-value-lg .status-chip .dot { width: 11px; height: 11px; margin-top: .3em; }

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

  /* ③ market table + cards */
  table.market-table { margin-bottom: 1.2rem; }
  .card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 1rem; }
  .query-card { border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.2rem; background: var(--market-surface); }
  .query-card > summary { cursor: pointer; display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; list-style: none; }
  .query-card > summary::-webkit-details-marker { display: none; }
  .query-card .stat-row { grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); margin: .8rem 0 .6rem; }
  .domains { margin: .6rem 0; }
  .competitor-note { font-size: .8rem; color: var(--text-secondary); margin: .4rem 0; display: flex; gap: .5rem; align-items: baseline; }
  .match-condition { font-size: .74rem; color: var(--text-muted); margin-top: .5rem; }
  .variants { margin: .6rem 0; }
  .variant-list { list-style: none; padding: 0; margin: .3rem 0 0; font-size: .78rem; color: var(--text-secondary); max-height: 220px; overflow-y: auto; }
  .variant-list li { padding: .1rem 0; }
  .verdict { border-top: 1px solid var(--gridline); margin-top: .8rem; padding-top: .7rem; }
  .judgment-line { font-size: .85rem; margin-top: .4rem; color: var(--text-secondary); }

  /* generic finding list (⑤ initiatives, ⑥ priority actions) */
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
  .datastudio-link { text-align: center; padding: 1.6rem 1.4rem; }
  .datastudio-button { display: inline-block; padding: .9rem 1.4rem; background: var(--text-primary); color: var(--surface-1); font-size: 1.05rem; font-weight: 600; text-decoration: none; border-radius: 6px; letter-spacing: .02em; }
</style>
"""


def render_html(vm: dict) -> str:
    header = vm["header"]
    period = header["period"]

    body = f"""
<div class="report-root">
<h1>TCD週次SEO/AIOレポート（{escape(period['cur_start'])} 〜 {escape(period['cur_end'])}）</h1>
<p class="note">対象期間: {escape(period['cur_start'])}〜{escape(period['cur_end'])}（データ確定基準: 実行日-3日オフセット） ／ データ品質: {escape(header['data_quality_status'])} ／ 継続週数: {escape(str(header.get('weeks_available', '-')))}週</p>

<section>
  <h2>① サイトアクセス状況</h2>
  {_site_usage_html(vm["site_usage"])}
</section>

<section class="executive-summary">
  <h2>② 今週の結論（Executive Summary）</h2>
  {_executive_summary_html(vm["executive_summary"])}
</section>

<section>
  <h2>③ 今週やること</h2>
  {_this_week_actions_html(vm["this_week_actions"])}
</section>

<section>
  <h2>④ 重点市場</h2>
  <div class="lede">TCDが追っているのは個別ページの順位そのものではなく、4つの重点市場でAIO・SEO双方で勝てているかどうか。詳細は各市場カードを開いて確認する。</div>
  {_priority_markets_html(vm["priority_markets"])}
</section>

<section>
  <h2>⑤ カテゴリ別アクセス動向</h2>
  {_category_page_trends_html(vm["category_page_trends"], header["low_impressions_threshold"])}
</section>

<section>
  <h2>⑥ 施策評価</h2>
  {_initiatives_html(vm["initiatives"])}
</section>

{_appendix_html(vm["appendix"], header)}

<section class="datastudio-link">
  <a class="datastudio-button" href="https://datastudio.google.com/reporting/4d67c760-d3dc-4a1e-b2d2-47ce3b44f69a/page/p_w254n6vx5d">Data Studio で確認する →</a>
</section>
</div>
"""

    return (
        f"<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>TCD週次SEO/AIOレポート {escape(period['cur_start'])}〜{escape(period['cur_end'])}</title>"
        f"{_STYLE}</head><body>{body}</body></html>"
    )
