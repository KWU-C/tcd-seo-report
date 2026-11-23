"""表示層の共通View Model。

analysis.json を主入力とする。表示用の閾値（順位変化の横ばい/軽微/大幅の3段階、
Impressions下限）のみ config_v2/thresholds.yaml から読む（新しい分析判定・
閾値判定は追加しない。Finding.severity/confidence/judgmentは既にanalysisレイヤーで
確定済みの値をそのまま使い、ここでは「どれを本文に出すか／どうグルーピングするか」
という表示上の取捨選択のみ行う）。

レポート構成（5セクション + Appendix。2026-10-13改善指示書で「全体アクセス → SEO順位 → AIO引用 → 詳細」
の流れに再構成し、2026-10-27に②問い合わせ実績を追加。表示順の正本はrender_html.py/render_md.py）:
  ① サイト全体のアクセス状況（GA4） … 1段目: サイト全体/マガジン/Works/Service、2段目: 問い合わせ/資料請求
  ② 問い合わせ実績                  … config_v2/inquiries.yaml の確定済み件数（当週・直近4週累計。未入力は0件にしない）
  ③ 今週の要点                      … 最大3項目（アクセス → SEO順位 → AIO引用）。数値の羅列ではなく変化を書く
  ④ SEO検索順位                     … 重点4キーワードを1つの表で表示（順位・前週比・Imp・Click）
  ⑤ AIO引用状況                     … 同じ4キーワードのAIO生成／TCD引用／TCD推薦／主な引用元（最大3件）を独立した表で表示
  Appendix                          … キーワード別詳細（クエリ内訳・AIO引用元全件・判定）・ページ別・未登録施策・データ品質等
重点商談市場（deal）／テーマ市場（theme）の区分は内部データ（tier）として残し、見出しには出さない。
参考クエリ（インナーブランディング会社×地域）はAppendixのキーワード別詳細に1行で表示する。

render_md.py / render_html.py / render_json.py は、この関数が返す同一の
View Modelだけを参照することで、出力形式間の内容差異を防ぐ。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from src_v2 import config_loader
from src_v2.report import content_eval

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
    """旧方式（analysis.json に access が無い週）の①。過去のレポートを同じ内容で再生成するため残す。"""
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


METRIC_USERS_LABEL = "アクティブユーザー数"
METRIC_SESSIONS_LABEL = "セッション数"
SITE_USAGE_METRIC_NOTE = ("到達ユーザーの指標はGA4の「アクティブユーザー数」（activeUsers）。各行は、その条件に合うページを"
                          "期間中に1回以上表示したユーザー数で、複数の行に重複して数えるため合計はサイト全体と一致しない。")
CHANNEL_NOTE = ("流入元の指標はGA4の「セッション数」（sessions。訪問の回数）で、上の到達ユーザー（人数）とは単位が違う。"
                "参照元／メディアごとに1つの区分に入れるため、区分の合計はサイト全体のセッション数と一致する。"
                "エンゲージメント率は、10秒以上の滞在・2ページ以上の閲覧・コンバージョンのいずれかがあったセッションの割合。")
RULE_CHANGE_NOTE = ("2026-11-23配信分から、ページの集計条件をURLの部分一致から完全一致・前方一致に変えた"
                    "（/service・/works のトップを含め、/works/service や /b2b/contact などの混入を除いた。"
                    "/download と /b2b/download は別の行）。11/16配信分までのレポートの値とは一致しない。"
                    "前週の値とセグメント別の推移は、過去の週も新しい条件で集計し直した値。")
TREND_NOTE = ("「広告を除く」アクティブユーザー数は、広告以外の流入元からのセッションが週に1回以上あったユーザー数"
              "（広告と広告以外の両方から来たユーザーも含む）。セッション数は広告の区分を除いた数。最新週は速報値。")


def _anomaly_note(seg: dict, a: dict) -> str:
    day = _md(a["date"])
    if a["period"] == "current":
        return (f"{seg['label']}: {day}の1日に{a['day_users']:,}人（日別の合計{a['days_total']:,}人、"
                f"週の{METRIC_USERS_LABEL}{a['week_users']:,}人）。原因は特定していない。異常値として扱い、前週比は評価に使わない。")
    return (f"{seg['label']}: 前週の値に、{day}の1日に集中した異常値（{a['day_users']:,}人）を含む。"
            "原因は特定していない。前週比は評価に使わない。")


def _build_trend(trend: dict | None) -> dict | None:
    if not trend:
        return None
    if trend.get("status") != "ok":
        return {"status": trend.get("status"), "error": trend.get("error"), "rows": []}
    rows = [{"week_label": f"{_md(w['week_start'], weekday=False)}〜", "week_start": w["week_start"],
             "users_total": w["users_total"], "users_non_ad": w["users_non_ad"],
             "sessions_total": w["sessions_total"], "sessions_ad": w["sessions_ad"], "sessions_non_ad": w["sessions_non_ad"]}
            for w in trend["weeks"]]
    return {"status": "ok", "error": None, "rows": rows}


def _build_segment_trend(st: dict | None, segments: list[dict]) -> dict | None:
    if not st:
        return None
    weeks = st["weeks"]
    rows = []
    for s in segments:
        seg = (st.get("segments") or {}).get(s["key"])
        if not seg:
            continue
        rows.append({"label": s["label"], "error": seg.get("error"),
                     "values": [seg["values"].get(w) for w in weeks] if not seg.get("error") else []})
    return {"week_labels": [f"{_md(w, weekday=False)}〜" for w in weeks], "rows": rows}


def _legacy_note(legacy: dict | None, segments: list[dict]) -> str | None:
    """移行期間の注記: 旧定義（部分一致）と新定義での今週の値。"""
    if not legacy:
        return None
    by_key = {s["key"]: s for s in segments}
    parts = []
    for key, v in legacy.items():
        s = by_key.get(key)
        if not s or v.get("current") is None or s.get("current") is None:
            continue
        parts.append(f"{s['label']} {v['current']:,}→{s['current']:,}人")
    if not parts:
        return None
    return "集計条件の変更による差（今週の値。旧条件の部分一致→新条件）: " + "、".join(parts) + "。"


# ① 冒頭のサマリー（旧版と同じ6カード。1段目4枚・2段目2枚）。値は詳細の表と同じセグメント（新しい集計条件）を参照する
SUMMARY_TILE_ROWS = (("site_wide", "magazine", "works", "service"), ("contact", "document_request"))


def _summary_tiles(segments: list[dict]) -> list[list[dict]]:
    by_key = {s["key"]: s for s in segments}
    rows = [[by_key[k] for k in row if k in by_key] for row in SUMMARY_TILE_ROWS]
    return [r for r in rows if r]


def _build_access(access: dict, notes_cfg: list[dict], cur_end: str) -> dict:
    """現行方式の①（2026-11-23週〜）。"""
    segments = access["segments"]
    anomaly_notes = [_anomaly_note(s, a) for s in segments for a in s["anomalies"]]
    groups = [("main", "サイト全体・主要セクション"), ("cv", "問い合わせ・資料請求")]
    rows = [[s for s in segments if s["group"] == g] for g, _ in groups]
    return {
        "scheme": "v2",
        "metric_label": METRIC_USERS_LABEL,
        "segments": segments,
        "rows": [r for r in rows if r],
        "summary_tiles": _summary_tiles(segments),
        "anomaly_notes": anomaly_notes,
        "metric_note": SITE_USAGE_METRIC_NOTE,
        "rule_notes": [n for n in (RULE_CHANGE_NOTE, _legacy_note(access.get("legacy_comparison"), segments)) if n],
        "segment_trend": _build_segment_trend(access.get("segment_trend"), segments),
        "channels": access.get("channels"),
        "channel_note": CHANNEL_NOTE,
        "trend": _build_trend(access.get("trend")),
        "trend_note": TREND_NOTE,
        "extra_notes": _period_notes(notes_cfg, "site_usage", cur_end),
    }


# ── ② 問い合わせ実績（config_v2/inquiries.yaml の確定済み件数） ──────────

# 表示順。「うち」は一般問い合わせの内訳。専用採用フォームは一般問い合わせとは別集計
_INQUIRY_ROWS = [
    ("general_total", "一般問い合わせ"),
    ("project_consultation", "うち案件相談"),
    ("sales_solicitation", "うち売り込み・提携勧誘"),
    ("other", "うちその他"),
    ("recruiting_form", "専用採用フォーム（別集計）"),
]
_INQUIRY_CUMULATIVE_WEEKS = (4, 12)
INQUIRY_NOTES = [
    "Dot・担当者が問い合わせ通知メールで確認した件数（通知メールへの返信は除く）。サーバー側の未着分は未確認。",
    "分類はフォームの選択項目ではなく本文の内容による。判断が難しいものは「その他（要確認）」に含める。",
    "案件相談は週ごとの変動が大きいため、施策の評価には4週・12週の累計を使う。累計は対象週がすべて入力済みの場合のみ表示する。",
]


def _week_windows(cur_start: str, weeks: int) -> list[tuple[str, str]]:
    """対象週を末尾とする連続した7日間 weeks 個（古い順）。"""
    start = date.fromisoformat(cur_start)
    return [((start - timedelta(days=7 * k)).isoformat(), (start - timedelta(days=7 * k - 6)).isoformat())
            for k in reversed(range(weeks))]


def _build_inquiries(inquiries_cfg: list[dict], period: dict) -> dict:
    """旧方式（〜2026-10-30、レポート週を持たない analysis.json）の②。過去のレポートを同じ内容で再生成するため残す。
    当週と直近4週・12週累計。未入力の週は0件として扱わない（当週は「未入力」、累計は欠けがあれば未確定）。"""
    by_window = {(w["start"], w["end"]): w for w in inquiries_cfg}
    current = by_window.get((period["cur_start"], period["cur_end"]))

    cumulative = {}
    for weeks in _INQUIRY_CUMULATIVE_WEEKS:
        entries = [by_window.get(window) for window in _week_windows(period["cur_start"], weeks)]
        entered = [e for e in entries if e is not None]
        complete = len(entered) == weeks
        cumulative[weeks] = {
            "weeks": weeks,
            "entered_weeks": len(entered),
            "complete": complete,
            "counts": {key: sum(e[key] for e in entered) for key, _ in _INQUIRY_ROWS} if complete else None,
            "other_needs_review": sum(e["other_needs_review"] for e in entered) if complete else None,
        }

    rows = []
    for key, label in _INQUIRY_ROWS:
        rows.append({
            "key": key, "label": label,
            "current": current[key] if current else None,
            "cumulative_4w": cumulative[4]["counts"][key] if cumulative[4]["complete"] else None,
            "cumulative_12w": cumulative[12]["counts"][key] if cumulative[12]["complete"] else None,
        })
    notes = []
    if current is None:
        notes.append("当週の件数は未入力（config_v2/inquiries.yaml）。0件ではない。")
    elif current["other_needs_review"]:
        notes.append(f"その他のうち要確認: {current['other_needs_review']}件")
    case_12w = next(r for r in rows if r["key"] == "project_consultation")["cumulative_12w"]
    notes.append(f"直近12週累計の案件相談: {inquiry_cumulative_label(cumulative[12], case_12w)}")
    return {
        "entered": current is not None,
        "rows": rows,
        "current_needs_review": current["other_needs_review"] if current else None,
        "cumulative": cumulative,
        "notes": notes + INQUIRY_NOTES,
    }


# ── ② 問い合わせ実績（現行: 月〜日。スプレッドシート＋手入力） ──────────

# 週ごとの状態。confirmed 以外は件数を出さず「未集計」と表示し、累計にも入れない（0件として扱わない）
INQUIRY_CONFIRMED_MANUAL = "confirmed_manual"
INQUIRY_CONFIRMED_SHEET = "confirmed_sheet"
INQUIRY_NOT_COUNTED = "not_counted"          # 行が無い・集計中
INQUIRY_INVALID = "invalid"                  # スプレッドシートの行が検査に通らない
INQUIRY_FETCH_FAILED = "fetch_failed"        # スプレッドシートを取得できない
INQUIRY_NOT_CONFIGURED = "not_configured"    # スプレッドシートが未設定

_INQUIRY_STATUS_LABELS = {
    INQUIRY_CONFIRMED_MANUAL: "確定（手入力）",
    INQUIRY_CONFIRMED_SHEET: "確定（Dot集計）",
    INQUIRY_NOT_COUNTED: "未集計（Dotの集計が未完了。0件ではない）",
    INQUIRY_INVALID: "未集計（スプレッドシートの集計値に不整合があり確認中。0件ではない）",
    INQUIRY_FETCH_FAILED: "未集計（スプレッドシートを取得できなかった。0件ではない）",
    INQUIRY_NOT_CONFIGURED: "未集計（スプレッドシートが未設定。0件ではない）",
}

FORM_NOTE = ("フォーム区分は、送信されたフォームで 一般（/contact）／BtoB（/b2b/contact）／不明 に分けた件数。"
             "一般問い合わせ・案件相談の件数の内訳で、合計は上の表と一致する。")

WEEKLY_INQUIRY_NOTES = [
    "Dotが問い合わせ通知メールで確認・分類した件数（通知メールへの返信は除く）。サーバー側の未着分は未確認。",
    "分類はフォームの選択項目ではなく本文の内容による。判断が難しいものは「その他（要確認）」に含める。",
    "案件相談は週ごとの変動が大きいため、施策の評価には4週・12週の累計を使う。累計は対象週がすべて集計済みの場合のみ表示する。",
]


def _resolve_inquiry_week(start: str, end: str, manual: dict, sheet_rows: dict, snapshot: dict | None) -> dict:
    """1週分の値と状態。優先順: 手入力（inquiries.yaml）→ スプレッドシートの確定行 → 未集計。"""
    from src_v2.fetch_inquiries import ROW_CONFIRMED, ROW_INVALID, SNAPSHOT_FETCH_FAILED, SNAPSHOT_NOT_CONFIGURED

    row = sheet_rows.get(start)
    entry = manual.get((start, end))
    warnings = []
    if entry is not None:
        if row and row["state"] == ROW_CONFIRMED and any(row[k] != entry[k] for k, _ in _INQUIRY_ROWS):
            warnings.append(f"{start}〜{end}: 手入力とスプレッドシートの値が異なるため、手入力を使用")
        return {"start": start, "end": end, "status": INQUIRY_CONFIRMED_MANUAL, "values": entry, "warnings": warnings}
    snapshot_status = (snapshot or {}).get("status")
    if row and row["state"] == ROW_CONFIRMED:
        return {"start": start, "end": end, "status": INQUIRY_CONFIRMED_SHEET, "values": row, "warnings": warnings,
                "confirmed_at": row.get("confirmed_at")}
    if row and row["state"] == ROW_INVALID:
        warnings.append(f"{start}〜{end}: スプレッドシートの集計値に不整合（{'／'.join(row['errors'])}）")
        return {"start": start, "end": end, "status": INQUIRY_INVALID, "values": None, "warnings": warnings}
    if snapshot is None or snapshot_status == SNAPSHOT_NOT_CONFIGURED:
        status = INQUIRY_NOT_CONFIGURED
    elif snapshot_status == SNAPSHOT_FETCH_FAILED:
        status = INQUIRY_FETCH_FAILED
    else:
        status = INQUIRY_NOT_COUNTED
    return {"start": start, "end": end, "status": status, "values": None, "warnings": warnings}


def _rate_label(consultation: int | None, total: int | None) -> str | None:
    if consultation is None or total is None:
        return None
    if total == 0:
        return "—（一般問い合わせ0件）"
    return f"{consultation / total * 100:.1f}%（{consultation}/{total}）"


_FORM_STATE_LABELS = {
    "not_recorded": "未記録（スプレッドシートのフォーム区分の列が空）",
    "invalid": "不整合（フォーム区分の合計が既存の件数と合わないため表示しない）",
}


def _build_forms(values: dict | None) -> dict:
    """当週のフォーム区分（一般／BtoB／不明）。未記録・不整合は件数を出さない（0件として扱わない）。"""
    if not values:
        return {"state": None, "label": None, "rows": []}
    forms = values.get("forms") or {"state": "not_recorded", "errors": []}
    if forms["state"] != "ok":
        return {"state": forms["state"], "label": _FORM_STATE_LABELS[forms["state"]], "rows": [],
                "errors": forms.get("errors", [])}
    rows = [{"label": label, **forms[key]} for key, label in
            (("general_total", "一般問い合わせ"), ("project_consultation", "うち案件相談"))]
    return {"state": "ok", "label": "記録あり", "rows": rows, "errors": []}


def _contact_reach_note(site_usage: dict | None, values: dict | None) -> str | None:
    """②の参考行: ①のフォーム到達（人数）と、問い合わせ・案件相談（件数）を並べる。単位が違うことを明記する。"""
    if not site_usage or site_usage.get("scheme") != "v2":
        return None
    seg = {s["key"]: s for s in site_usage["segments"]}
    reach = []
    for key, label in (("contact", "一般"), ("b2b_contact", "BtoB")):
        s = seg.get(key)
        if s and s.get("current") is not None:
            reach.append(f"{label}{s['current']:,}人")
    if not reach:
        return None
    tail = (f" → 一般問い合わせ{values['general_total']:,}件 → うち案件相談{values['project_consultation']:,}件"
            if values else " → 問い合わせ件数は未集計")
    return (f"参考（同じ週）: 問い合わせフォーム到達（{METRIC_USERS_LABEL}）{'・'.join(reach)}{tail}。"
            "フォーム到達は人数、問い合わせは件数で、単位が違う（到達数から送信率は計算しない）。")


def _build_weekly_inquiries(inquiries_cfg: list[dict], snapshot: dict | None, period: dict,
                            site_usage: dict | None = None) -> dict:
    """現行方式の②（レポート週＝月〜日）。未集計でも生成は止めず、状態を明示する。"""
    manual = {(w["start"], w["end"]): w for w in inquiries_cfg
              if w.get("scheme", config_loader.INQUIRY_SCHEME_WEEKLY) == config_loader.INQUIRY_SCHEME_WEEKLY}
    sheet_rows = {r["week_start"]: r for r in (snapshot or {}).get("rows", [])}
    week_start = period["report_week_start"]

    resolved = {}
    for window in _week_windows(week_start, max(_INQUIRY_CUMULATIVE_WEEKS)):
        resolved[window] = _resolve_inquiry_week(*window, manual, sheet_rows, snapshot)
    current = resolved[(week_start, period["report_week_end"])]
    confirmed = {INQUIRY_CONFIRMED_MANUAL, INQUIRY_CONFIRMED_SHEET}

    cumulative = {}
    for weeks in _INQUIRY_CUMULATIVE_WEEKS:
        entries = [resolved[w] for w in _week_windows(week_start, weeks)]
        done = [e["values"] for e in entries if e["status"] in confirmed]
        complete = len(done) == weeks
        cumulative[weeks] = {
            "weeks": weeks, "entered_weeks": len(done), "complete": complete, "done_label": "集計済み",
            "counts": {key: sum(e[key] for e in done) for key, _ in _INQUIRY_ROWS} if complete else None,
            "other_needs_review": sum(e["other_needs_review"] for e in done) if complete else None,
        }

    values = current["values"] if current["status"] in confirmed else None
    rows = [{
        "key": key, "label": label,
        "current": values[key] if values else None,
        "cumulative_4w": cumulative[4]["counts"][key] if cumulative[4]["complete"] else None,
        "cumulative_12w": cumulative[12]["counts"][key] if cumulative[12]["complete"] else None,
    } for key, label in _INQUIRY_ROWS]

    by_key = {r["key"]: r for r in rows}
    rate = {
        "label": "案件相談率（案件相談÷一般問い合わせ）",
        "current_label": _rate_label(by_key["project_consultation"]["current"], by_key["general_total"]["current"]),
        "cumulative_4w_label": (_rate_label(by_key["project_consultation"]["cumulative_4w"], by_key["general_total"]["cumulative_4w"])
                                if cumulative[4]["complete"] else inquiry_cumulative_label(cumulative[4], None)),
    }
    forms = _build_forms(values)

    notes = []
    if values and values["other_needs_review"]:
        notes.append(f"その他のうち要確認: {values['other_needs_review']}件")
    case_12w = by_key["project_consultation"]["cumulative_12w"]
    notes.append(f"直近12週累計の案件相談: {inquiry_cumulative_label(cumulative[12], case_12w)}")
    reach = _contact_reach_note(site_usage, values)
    if reach:
        notes.append(reach)
    warnings = [w for e in resolved.values() for w in e["warnings"]]
    if forms["state"] == "invalid":
        warnings.append(f"{week_start}の週のフォーム区分: {'／'.join(forms['errors'])}")
    if snapshot and snapshot.get("error") and snapshot.get("status") != "ok":
        warnings.insert(0, f"問い合わせ集計スプレッドシート: {snapshot['error']}")
    return {
        "scheme": config_loader.INQUIRY_SCHEME_WEEKLY,
        "entered": values is not None,
        "status": current["status"],
        "status_label": _INQUIRY_STATUS_LABELS[current["status"]],
        "rows": rows,
        "rate": rate,
        "forms": forms,
        "current_needs_review": values["other_needs_review"] if values else None,
        "cumulative": cumulative,
        "notes": notes + WEEKLY_INQUIRY_NOTES + [FORM_NOTE],
        "warnings": warnings,
    }


def inquiry_cumulative_label(cum: dict, value: int | None) -> str:
    """累計セルの表示。欠けがある期間は件数を出さず「未確定（4週中1週入力）」とする。"""
    if cum["complete"]:
        return f"{value:,}"
    return f"未確定（{cum['weeks']}週中{cum['entered_weeks']}週{cum.get('done_label', '入力')}）"


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

# TCD推薦（AIO本文の会社一覧でTCDが推薦企業として紹介されたか）。引用とは独立に判定する。
# 「判定不可」（本文・一覧を取得できない、取得エラー）は「なし」に含めない。
_RECOMMENDATION_LABELS = {
    "recommended": "あり",
    "not_recommended": "なし",
    "undeterminable": "判定不可",
    "not_applicable": "対象外",  # 会社を探す検索ではない（「会社」を含まないキーワード）
    "no_aio": "—",
}


def _recommendation_label(aio: dict) -> str:
    """推薦判定がない（推薦列の導入前の analysis.json、AIO未取得）場合は「—」。"""
    if "recommendation_status" not in aio:
        return "—"
    return _RECOMMENDATION_LABELS.get(aio.get("recommendation_status"), "—")


def _aio_view(evidence: dict) -> dict:
    """⑤ AIO引用状況の1行分。AIO生成／TCD引用／TCD推薦／引用元を別々の列にする。
    未取得（観測なし・失敗）は「生成なし」と、引用元取得不可は「引用元なし」「TCD引用なし」と混同しない。"""
    aio = evidence.get("aio") or {}
    citations = evidence.get("aio_citations") or {}
    domains = citations.get("domains")
    if domains is None:  # aio_citations導入前のanalysis.json
        domains = list(dict.fromkeys(d for d in (aio.get("cited_domains") or []) if d and d != "tcd.jp"))

    unknown_count = citations.get("unknown_count") or 0
    view = {"aio_generated_label": "未取得", "tcd_cited_label": "—", "aio_sources_label": "—",
            "aio_domains": domains, "aio_unknown_source_count": unknown_count,
            "tcd_cited": None, "citation_unavailable": False,
            "tcd_recommended_label": _recommendation_label(aio), "tcd_recommended": aio.get("tcd_recommended")}
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

_INITIATIVE_ORDER = {"比較対照を下回る変化（原因未確定）": 0, "比較対照を上回る変化（原因未確定）": 1,
                     "指標で比較対照との差の向きが異なる（原因未確定）": 2,
                     "比較対照と異なる減少（原因未確定）": 0, "比較対照と異なる増加（原因未確定）": 1,
                     "指標で方向が異なる（原因未確定）": 2, "数値の変化のみ": 3, "効果確認待ち": 4, "評価不能": 5,
                     "検索流入の前後比較の対象外": 6, "評価対象未設定": 7}


def _fmt_ctr(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.2f}%"


def _fmt_pos(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def _fmt_change(new, old, kind: str) -> str:
    if new is None or old is None:
        return "—"
    if kind == "ctr":
        return f"{(new - old) * 100:+.2f}pt"
    if kind == "position":
        return f"{new - old:+.1f}"
    return f"{(new - old) / old * 100:+.1f}%" if old else ("±0" if new == old else "—（実装前0）")


def _initiative_view(f: dict) -> dict:
    """施策の効果確認の1件（現行方式の evidence を表示用に整える）。"""
    ev = f.get("evidence") or {}
    view = {"id": f["id"], "name": ev.get("name") or (f.get("source") or {}).get("initiative"),
            "verdict": f.get("judgment"), "reasons": ev.get("reasons") or [], "recommendation": f["recommendation"],
            "hypothesis": ev.get("hypothesis"), "primary_metric_label": (None if ev.get("evaluation_method") == "index"
                                     else "クリック率" if ev.get("primary_metric") == "ctr" else "表示回数・クリック数"),
            "implemented_at": ev.get("implemented_at"), "urls": ev.get("urls") or [], "metrics_rows": [], "period_label": None,
            "elapsed_label": None, "queries": None, "overlaps": ev.get("overlaps") or []}
    windows = ev.get("windows")
    if ev.get("implemented_at") and ev.get("gsc_end") and windows:
        view["elapsed_label"] = (f"実装日 {_md(ev['implemented_at'])} から{windows['elapsed_days']}日"
                                 f"（Search Consoleの確定済み最新日 {_md(ev['gsc_end'])} まで）")
        # 直近28日を使う施策は、実装後の初期期間を含まない（単純な実装直後との前後比較ではない）ことを明記する
        w = content_eval.comparison_window(ev)
        excluded = (f"実装後の初期期間 {_md(w['excluded']['start'])}〜{_md(w['excluded']['end'])} は含まない"
                    if w and w["excluded"] else "実装日の翌日からの期間")
        view["period_label"] = (f"実装前{windows['days']}日間 {_md(windows['pre']['start'])}〜{_md(windows['pre']['end'])} と "
                                f"直近{windows['days']}日間 {_md(windows['post']['start'])}〜{_md(windows['post']['end'])}。"
                                f"実装日 {_md(windows['excluded_day'])} は公開前後が混ざるため除く。{excluded}")
    if ev.get("metrics"):
        pre, post = ev["metrics"]["pre"], ev["metrics"]["post"]
        cpre, cpost = (ev.get("control") or {}).get("pre"), (ev.get("control") or {}).get("post")
        for key, label, fmt in (("impressions", "表示回数", lambda v: f"{v:,}"), ("clicks", "クリック数", lambda v: f"{v:,}"),
                                ("ctr", "クリック率", _fmt_ctr), ("position", "平均掲載順位（参考・小さいほど上位）", _fmt_pos)):
            control = "—"
            if cpre and cpost and key in cpre:
                control = _fmt_change(cpost[key], cpre[key], key)
            view["metrics_rows"].append({"label": label, "pre": fmt(pre[key]), "post": fmt(post[key]),
                                         "change": _fmt_change(post[key], pre[key], key), "control_change": control})
    q = ev.get("queries")
    if q:
        share = q.get("shared_share")
        view["queries"] = {
            "shared_label": "—" if share is None else f"{share * 100:.0f}%",
            "counts_label": f"実装前{q['pre_query_count']}件 → 実装後{q['post_query_count']}件",
            "top": [f"「{x['query']}」{x['impressions']:,}回（{_fmt_pos(x['position'])}位）" for x in q["top_post"]],
            "increased": [f"「{x['query']}」{x['pre']:,}→{x['post']:,}" for x in q["increased"]],
            "decreased": [f"「{x['query']}」{x['pre']:,}→{x['post']:,}" for x in q["decreased"]],
        }
    return view


INITIATIVE_NOTES = [
    "実装前28日間と直近28日間を比べる（実装後が28日未満の間は実装前後の同じ日数。実装日当日は除く）。実装から28日を超えた施策では、"
    "実装後の初期期間を含まないため、単純な実装直後との前後比較ではない。今週と先週の比較では施策を評価しない。",
    "「数値の変化」は対象ページの値そのもの。「比較対照の変化」は同じ期間の対象ページ以外（サイト全体−対象ページ）の変化で、"
    "季節・曜日・サイト全体の増減など、施策以外の要因の目安。",
    "判定は観測した事実の分類で、因果関係の判定ではない。「比較対照を上回る／下回る変化（原因未確定）」は、対象ページの変化が"
    "比較対照の変化と日々の変動では説明しにくいことを示すだけで（「下回る」は対象ページの値が減ったことを意味しない）、施策の効果・悪影響とは判断していない（原因の確認は担当者）。"
    "固定の増減率や順位で成功・失敗を決めない。",
    "表示回数・クリック数が少ない指標（実装前・実装後のどちらかが表示回数50未満・クリック5未満）は、日々の変動と区別できないため評価しない。",
    "平均掲載順位は数値が小さいほど上位。検索クエリの構成で変わるため判定に使わず、参考として示す（比較対照の平均掲載順位は計算しない）。",
    "正規化（canonical）・インデックスの施策は、表示回数・クリック率の前後比較では判定しない（URL検査・インデックス登録レポートで確認する）。",
]


def _build_initiatives_section(initiative_findings: list[dict]) -> tuple[dict, list[dict]]:
    known = [f for f in initiative_findings if "implemented_at" in (f.get("evidence") or {})]
    unknown = [f for f in initiative_findings if f not in known]
    return {"known": sort_findings(known), "unknown_count": len(unknown)}, unknown


# ── ④ SEO検索順位の前週比表示 ─────────────────────────────────────

RELATED_QUERIES_NOTE = "関連クエリを含む集計"
_RECOMMENDATION_NOTE = ("TCD推薦は、AIO本文の会社一覧でTCDが推薦企業として紹介されたか（引用とは別に判定）。"
                        "「判定不可」は本文・会社一覧を取得できなかった場合で、「なし」には含めない。"
                        "「対象外」は会社を探す検索ではないキーワード。")
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


# ── ③ 今週の要点 ────────────────────────────────────────────────

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


def _access_highlight_v2(site_usage: dict, thresholds_cfg: dict) -> str | None:
    """①の要約（現行方式）。指標名を明記し、広告を除いた値と、異常値を含むセグメントの扱いを書く。
    数値の分解（どの区分が何件増えたか）は書くが、原因は推測しない。"""
    change_pct = thresholds_cfg["display"]["access_change_pct"]
    segments = [s for s in site_usage["segments"] if not s.get("error") and s.get("current") is not None]
    site = next((s for s in segments if s["key"] == "site_wide"), None)
    if site is None:
        return None
    delta = site.get("delta_pct")
    parts = [f"サイト全体の{METRIC_USERS_LABEL}は{site['current']:,}" + (f"（前週比{_signed(delta)}%）" if delta is not None else "")]
    trend = site_usage.get("trend") or {}
    if trend.get("status") == "ok" and len(trend["rows"]) >= 2:
        cur, prv = trend["rows"][-1], trend["rows"][-2]
        if prv["users_non_ad"]:
            d = (cur["users_non_ad"] - prv["users_non_ad"]) / prv["users_non_ad"] * 100
            parts.append(f"広告を除くと{cur['users_non_ad']:,}（前週比{_signed(d)}%）")
    line = "、".join(parts) + "。"

    channels = site_usage.get("channels") or {}
    if channels.get("status") == "ok" and channels.get("total"):
        diff = channels["total"]["current"] - channels["total"]["comparison"]
        movers = sorted(channels["rows"], key=lambda r: -abs(r["current"] - r["comparison"]))
        top = movers[0] if movers else None
        if top and diff and top["current"] != top["comparison"]:
            line += (f"{METRIC_SESSIONS_LABEL}の前週差{diff:+,}のうち、最も大きいのは{top['label']}の"
                     f"{top['current'] - top['comparison']:+,}。")

    anomalous = {s["key"] for s in segments if s.get("anomalies")}
    min_users = thresholds_cfg["display"].get("access_min_users", 0)
    # 少人数のセグメント（例: 6→8人）の増減は、要約で「増加・減少」と書かない（表には出す）
    others = [s for s in segments if s["key"] != "site_wide" and s.get("delta_pct") is not None and s["key"] not in anomalous
              and max(s["current"] or 0, s["comparison"] or 0) >= min_users]
    up = [s["label"].replace("到達", "") for s in others if s["delta_pct"] >= change_pct]
    down = [s["label"].replace("到達", "") for s in others if s["delta_pct"] <= -change_pct]
    changes = []
    if up:
        changes.append(f"{'・'.join(up)}への到達は増加")
    if down:
        changes.append(f"{'・'.join(down)}への到達は減少")
    if changes:
        line += "、".join(changes) + "。"
    for s in segments:
        for a in s.get("anomalies") or []:
            when = "今週" if a["period"] == "current" else "前週"
            line += f"{s['label']}は{when}の{_md(a['date'])}の1日に集中した異常値（原因未確認）を含むため、増減を評価しない。"
    return line


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
        line = f"AIOは{'・'.join(cited)}でTCD引用あり" + (f"（{unavailable_note}）" if unavailable_note else "") + "。"
    else:
        line = f"定点観測{len(rows)}クエリではTCDのAIO引用を確認できず" + (f"（うち{unavailable_note}）" if unavailable_note else "") + "。"
    return line + _recommendation_highlight(rows)


def _recommendation_highlight(rows: list[dict]) -> str:
    """推薦を判定するキーワード（会社を探す検索）だけ。判定不可は「なし」と書かない。"""
    by_label: dict[str, list[str]] = {}
    for r in rows:
        if r["tcd_recommended_label"] in ("あり", "なし", "判定不可"):
            by_label.setdefault(r["tcd_recommended_label"], []).append(r["short_label"])
    parts = []
    if by_label.get("あり"):
        parts.append(f"{'・'.join(by_label['あり'])}でTCD推薦あり")
    if by_label.get("なし"):
        parts.append(f"{'・'.join(by_label['なし'])}は推薦なし")
    if by_label.get("判定不可"):
        parts.append(f"{'・'.join(by_label['判定不可'])}は推薦を判定不可")
    return ("、".join(parts) + "。") if parts else ""


def _build_highlights(site_usage: dict, priority_markets: dict, thresholds_cfg: dict) -> list[str]:
    """今週の要点（最大3項目）。優先順位は アクセス → SEO順位 → AIO引用（2026-10-13改善指示書）。
    analysis.jsonの数値から機械的に組み立て、原因の推測・提案は書かない。"""
    rows = priority_markets["table"]
    access = (_access_highlight_v2(site_usage, thresholds_cfg) if site_usage.get("scheme") == "v2"
              else _access_highlight(site_usage, thresholds_cfg))
    lines = [access, _seo_highlight(rows), _aio_highlight(rows)]
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


_WEEKDAYS = "月火水木金土日"


def _md(iso: str, weekday: bool = True) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}({_WEEKDAYS[d.weekday()]})" if weekday else f"{d.month}/{d.day}"


def _build_periods(period: dict, ga4_revision: dict | None) -> dict:
    """レポートの対象期間の表示。現行方式（レポート週あり）はデータごとに期間を分けて明記する。
    旧方式の analysis.json は従来どおり1つの期間（GSC＝GA4）で表示する。"""
    if "report_week_start" not in period:
        return {
            "scheme": "legacy",
            "title": f"{period['cur_start']} 〜 {period['cur_end']}",
            "header_note": f"対象期間: {period['cur_start']}〜{period['cur_end']}（データ確定基準: 実行日-3日オフセット）",
            "folder": f"{period['cur_start']}_{period['cur_end']}",
            "sections": {"site_usage": "", "inquiries": "", "seo": "", "aio": ""},
            "notes": {"site_usage": [], "seo": []},
        }
    week = f"{_md(period['report_week_start'])}〜{_md(period['report_week_end'])}"
    gsc = f"{_md(period['cur_start'])}〜{_md(period['cur_end'])}"
    observed = period.get("aio_observed_at")
    aio = ""
    if observed:
        ts = observed[:16]
        aio = f"{_md(ts[:10])} {ts[11:16]} 観測"
    parts = [f"アクセス・問い合わせ: {week}", f"検索順位: {gsc}（Search Consoleの確定済み最新7日）"]
    if aio:
        parts.append(f"AIO: {aio}")
    seo_notes = [f"検索順位はSearch Consoleで確定済みの最新7日間（{gsc}、米国太平洋時間）。前週比は直前の7日間（{_md(period['prv_start'])}〜{_md(period['prv_end'])}）との比較。"]
    if period.get("gsc_shift_days"):
        seo_notes.append(f"Search Consoleのデータ確定が遅れているため、通常より{period['gsc_shift_days']}日前までの期間で集計した。")
    site_notes = ["当週の値は速報値（日曜分はGA4の処理中の可能性がある）。前週の値は今回取り直した値。"]
    if period.get("ga4_hostnames"):
        site_notes.append(f"本番サイト（{'・'.join(period['ga4_hostnames'])}）のアクセスのみ。ステージング・バックアップ環境のアクセスは含めない。")
    return {
        "scheme": "weekly",
        "title": f"{period['report_week_start']} 〜 {period['report_week_end']}",
        "header_note": "対象期間: " + " ／ ".join(parts),
        "folder": f"{period['report_week_start']}_{period['report_week_end']}",
        "sections": {"site_usage": f"（{week}）", "inquiries": f"（{week}）", "seo": f"（{gsc}）", "aio": f"（{aio}）" if aio else ""},
        "notes": {"site_usage": site_notes, "seo": seo_notes},
        "ga4_revision": ga4_revision,
    }


def _data_quality_extra_lines(periods: dict, inquiries: dict) -> list[str]:
    """Appendix「データ品質」の追加行（現行方式のみ）: 期間の決め方・GA4速報値の再取得差・問い合わせ集計の警告。"""
    if periods["scheme"] != "weekly":
        return []
    lines = [periods["header_note"]]
    revision = periods.get("ga4_revision")
    if revision and revision.get("skipped") == "hostnames_changed":
        lines.append(f"前週GA4（{revision['period']['start']}〜{revision['period']['end']}）の速報値→再取得値: "
                     "集計対象を本番サイトのみに変えた週のため比べない")
    elif revision and revision.get("skipped") == "rules_changed":
        lines.append(f"前週GA4（{revision['period']['start']}〜{revision['period']['end']}）の速報値→再取得値: "
                     "ページの集計条件（完全一致・前方一致）を変えた週のため比べない")
    elif revision and revision.get("segments"):
        diffs = "、".join(
            f"{seg['label']} {seg['first']:,}→{seg['refetched']:,}"
            + (f"（{seg['delta_pct']:+.1f}%）" if seg.get("delta_pct") is not None else "")
            for seg in revision["segments"].values())
        lines.append(f"前週GA4（{revision['period']['start']}〜{revision['period']['end']}）の速報値→再取得値: {diffs}")
    lines.append(f"問い合わせ集計: {inquiries.get('status_label', '-')}")
    lines += [f"問い合わせ集計の警告: {w}" for w in inquiries.get("warnings", [])]
    return lines


def build_view_model(analysis: dict, thresholds_cfg: dict | None = None, notes_cfg: list[dict] | None = None,
                     inquiries_cfg: list[dict] | None = None) -> dict:
    thresholds_cfg = thresholds_cfg or config_loader.load_thresholds()
    notes_cfg = config_loader.load_report_notes() if notes_cfg is None else notes_cfg
    inquiries_cfg = config_loader.load_inquiries() if inquiries_cfg is None else inquiries_cfg
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
    # 現行方式（実装前後の比較、evidence に primary_metric がある）は全件を「施策の効果確認」に出す
    if any("primary_metric" in (f.get("evidence") or {}) for f in initiative_findings):
        initiatives_view = {
            "scheme": "v2",
            "items": [_initiative_view(f) for f in sorted(
                initiative_findings, key=lambda f: (_INITIATIVE_ORDER.get(f.get("judgment"), 9), f["id"]))],
            "notes": INITIATIVE_NOTES,
        }
    else:
        initiatives_view = {"scheme": "legacy", "items": unknown_initiatives, "notes": []}

    if analysis.get("access", {}).get("version"):
        site_usage = _build_access(analysis["access"], notes_cfg, analysis["period"]["cur_end"])
    else:
        site_usage = _build_site_usage(analysis.get("ga4_summary", {}))

    data_quality = analysis["data_quality"]
    data_summary = analysis.get("data_summary", {})

    periods = _build_periods(analysis["period"], analysis.get("ga4_revision"))
    if periods["scheme"] == "weekly":
        inquiries = _build_weekly_inquiries(inquiries_cfg, analysis.get("inquiries_snapshot"), analysis["period"], site_usage)
    else:
        inquiries = _build_inquiries(inquiries_cfg, analysis["period"])

    header = {
        "period": analysis["period"],
        "periods": periods,
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
        "initiatives": initiatives_view,
        "data_quality": {
            **data_quality,
            "label": header["data_quality_label"],
            "data_summary": data_summary,
            "gsc_counts": data_summary.get("gsc", {}),
            "aio_status": data_summary.get("aio", {}),
            "finding_count": analysis["counts"]["total"],
            "severity_breakdown": analysis["counts"]["by_severity"],
            "confidence_breakdown": confidence_breakdown,
            "extra_lines": _data_quality_extra_lines(periods, inquiries),
        },
    }

    # SEO/AIO・コンテンツ評価中心の構成（2026-11-23 再設計）。現行方式（レポート週・新しいアクセス集計）の週だけ
    layout = "legacy"
    content = None
    if periods["scheme"] == "weekly" and site_usage.get("scheme") == "v2":
        layout = "content"
        page_evals = content_eval.build_page_evaluations(
            page_findings, initiative_findings, priority_markets["table"], market_findings, thresholds_cfg)
        keyword_pages = content_eval.build_keyword_pages(priority_markets["table"], market_findings, page_findings,
                                                        thresholds_cfg["low_data"]["position_change_meaningful"])
        aio_eval = content_eval.build_aio_evaluation(keyword_pages, competitor_findings, market_findings,
                                                     analysis.get("aio_daily"))
        initiative_summary = content_eval.build_initiative_summary(
            initiative_findings, (thresholds_cfg.get("initiative_review") or {}).get("significance_t", 2.0))
        inquiry_dashboard = content_eval.build_inquiry_dashboard(inquiries)
        content = {
            "lead": content_eval.build_lead(keyword_pages, page_evals, aio_eval, initiative_summary,
                                            inquiry_dashboard, site_usage),
            "aio": aio_eval,
            "keyword_pages": keyword_pages,
            "pages": page_evals,
            "regional_lps": content_eval.build_regional_lp_summary(page_evals),
            "page_status_order": content_eval.STATUS_ORDER,
            "status_rules": content_eval.STATUS_RULES,
            "initiatives": initiative_summary,
            "inquiry_dashboard": inquiry_dashboard,
        }

    return {
        "layout": layout,
        "content": content,
        "schema_version": analysis["schema_version"],
        "generated_at": analysis["generated_at"],
        "header": header,
        "executive_summary": {"blocks": executive_summary, "narrative": None},
        "site_usage": site_usage,
        "inquiries": inquiries,
        "priority_markets": {
            **priority_markets,
            "search_volume_as_of": volume_as_of,
            "seo_notes": _seo_notes(volume_as_of),
            "aio_notes": _period_notes(notes_cfg, "aio", analysis["period"]["cur_end"]) + [_AIO_NOTE, _RECOMMENDATION_NOTE],
        },
        "appendix": appendix,
    }
