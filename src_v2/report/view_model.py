"""表示層の共通View Model。

analysis.json を主入力とする。表示用の閾値（順位変化の横ばい/軽微/大幅の3段階、
Impressions下限）のみ config_v2/thresholds.yaml から読む（新しい分析判定・
閾値判定は追加しない。Finding.severity/confidence/judgmentは既にanalysisレイヤーで
確定済みの値をそのまま使い、ここでは「どれを本文に出すか／どうグルーピングするか」
という表示上の取捨選択のみ行う）。

レポート構成（6セクション + Appendix。report.html改善指示書 3節。ユーザーフィードバックにより
セクション順を変更済み。表示順の正本はrender_html.py/render_md.pyの実装）:
  ① サイトアクセス状況（GA4）                … サイト全体/マガジン/Works/Service到達ユーザー
  ② 今週の結論（Executive Summary）      … 3ブロック（今週の評価/市場存在感/AIO、この順。見出しは「●」統一）
  ③ 今週やること（Priority Action）      … 最大3件
  ④ 重点市場（4市場固定）                … 一覧表 + 折りたたみ詳細カード
  ⑤ カテゴリ別アクセス動向               … カテゴリ別ロールアップ + 要確認ページのみ
  ⑥ 施策評価                             … 実装日が判明している施策のみ本文表示
  Appendix                               … 全Finding・低Confidence一覧・データ品質詳細等

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


def _annotate(finding: dict) -> dict:
    return {
        **finding,
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


# ── ② サイトアクセス状況（GA4） ─────────────────────────────────────

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
    return {"segments": segments}


# ── ③ 重点市場（4市場固定・一覧表 + 詳細カード） ──────────────────

_SEO_STATUS_LABELS = [
    (10, "上位表示"),
    (20, "1ページ目目前"),
    (30, "20位圏"),
    (None, "30位以下"),
]


def _seo_status_label(position: float | None, impressions: int) -> str:
    if position is None or impressions == 0:
        return "未露出"
    for threshold, label in _SEO_STATUS_LABELS:
        if threshold is None or position <= threshold:
            return label
    return "30位以下"


def _build_priority_markets(market_findings: list[dict], competitor_by_query: dict[str, list[dict]], thresholds_cfg: dict) -> dict:
    display_cfg = thresholds_cfg["display"]
    low_impressions = thresholds_cfg["low_data"]["low_impressions"]
    table = []
    cards = []
    for f in market_findings:
        evidence = f.get("evidence") or {}
        qid = (f.get("source") or {}).get("query_id")
        cur = evidence.get("current") or {}
        aio = evidence.get("aio") or {}
        position = cur.get("position")
        impressions = cur.get("impressions") or 0
        clicks = cur.get("clicks") or 0
        delta = _position_delta(evidence)
        # 表示回数が少ない場合は順位差を強調しない（report.html改善指示書 8節）
        tier = delta_tier(delta, display_cfg) if impressions >= low_impressions else "flat"

        aio_generated = aio.get("aio_generated")
        tcd_cited = aio.get("tcd_cited")
        if aio_generated and tcd_cited:
            aio_status = "引用あり"
        elif aio_generated:
            aio_status = "未引用"
        else:
            aio_status = "AIO未生成"

        competitor_summary = evidence.get("competitor_summary") or {}
        table.append({
            "market_label": f"{_MARKET_LABEL.get(evidence.get('market'), evidence.get('market'))} {_REGION_LABEL.get(evidence.get('region'), evidence.get('region'))}",
            "query": evidence.get("query"),
            "seo_status": _seo_status_label(position, impressions),
            "position": position,
            "position_delta": delta,
            "delta_tier": tier,
            "impressions": impressions,
            "clicks": clicks,
            "aio_status": aio_status,
            "competitor_comparison_available": competitor_summary.get("comparison_available", False),
            "judgment": f.get("judgment"),
        })

        cards.append({
            **f,
            "competitor_findings": competitor_by_query.get(qid, []),
            "position_delta": delta,
            "delta_tier": tier,
        })

    return {"table": table, "cards": cards}


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


# ── ⑥ 今週やること（Priority Action、最大3件） ────────────────────

def _action_target_label(f: dict) -> str:
    source = f.get("source") or {}
    if f["category"] == "market":
        return source.get("query", f["id"])
    if f["category"] == "competitor":
        return f"{source.get('query', '')}（競合動向）"
    if f["category"] == "page":
        evidence = f.get("evidence") or {}
        return evidence.get("title") or evidence.get("page_id") or f["id"]
    if f["category"] == "initiative":
        return source.get("initiative", f["id"])
    return f["id"]


def _action_score(f: dict, thresholds_cfg: dict) -> int:
    """優先順位ルール（report.html改善指示書 3節⑤）: 重点市場への直接影響／
    施策評価時期到達／十分な表示回数／問い合わせ導線への近さを加点する。
    複数週継続した変化（AIO競合）は、analysisレイヤーでseverity=warningまで
    昇格したものだけがこのactionable集合に入るため、単発変化は既に除外済み。
    """
    score = 0
    category = f["category"]
    evidence = f.get("evidence") or {}

    if category == "market":
        score += 3
    if category == "initiative":
        score += 3
    if category == "competitor":
        score += 2
    if category == "page" and "conversion" in (evidence.get("objective") or []):
        score += 1

    low_impressions = thresholds_cfg["low_data"]["low_impressions"]
    cur_impressions = (evidence.get("current") or {}).get("impressions")
    if cur_impressions is not None and cur_impressions >= low_impressions:
        score += 1

    return score


def _build_this_week_actions(all_sorted: list[dict], thresholds_cfg: dict, n: int = 3) -> list[dict]:
    actionable = [f for f in all_sorted if f.get("judgment") in _ACTIONABLE_JUDGMENTS]
    scored = sorted(actionable, key=lambda f: (-_action_score(f, thresholds_cfg), _sort_key(f)))
    picked = scored[:n]
    return [{**f, "action_target": _action_target_label(f)} for f in picked]


# ── ① 今週の結論（Executive Summary、下書き・数値ベース） ─────────

def _market_presence_label(row: dict) -> str:
    if row["impressions"] == 0:
        return "未露出"
    if row["delta_tier"] == "flat":
        return "横ばい"
    delta = row["position_delta"]
    if delta is not None and delta > 0:
        return "改善"
    return "悪化"


def _action_observation(f: dict) -> str:
    evidence = f.get("evidence") or {}
    cur = evidence.get("current") or {}
    if f["category"] in ("market", "page"):
        prv = evidence.get("comparison") or {}
        cur_pos, prv_pos = cur.get("position"), prv.get("position")
        if cur_pos is not None and prv_pos is not None:
            return f"{prv_pos:.1f}→{cur_pos:.1f}位（Imp {cur.get('impressions', 0)}, Click {cur.get('clicks', 0)}）"
        if cur_pos is not None:
            return f"{cur_pos:.1f}位（Imp {cur.get('impressions', 0)}, Click {cur.get('clicks', 0)}）"
        return "Search Console上でデータなし"
    if f["category"] == "initiative":
        return evidence.get("name", "-")
    if f["category"] == "competitor":
        return evidence.get("domain", "-")
    return f["display_message"]


def _action_evaluation(f: dict) -> str:
    judgment = f.get("judgment")
    if judgment == "再調査" and f["category"] in ("market", "page"):
        return "単週変動か継続的な変化かは未確定"
    if judgment == "判断保留":
        return "データ不足のため判断保留"
    if judgment == "再調査" and f["category"] == "competitor":
        return "複数週にわたる継続引用を確認"
    return f.get("judgment") or "-"


def _build_executive_summary(
    market_table: list[dict],
    this_week_actions: list[dict],
) -> list[str]:
    """今週の結論（下書き・数値ベース）。3ブロック（今週の評価/市場存在感/AIO、この順）。
    表示順がアルファベット順と一致しないため、見出しはA/B/C等の記号ではなく「●」で統一する。
    断定的な原因判定（「検索需要がない」等）は行わず、観測事実のみを述べる。
    「今週の評価」は各アクションを「・観測（→評価／→次）」の3行に絞り、毎回同じ原因候補の列挙はしない。
    （実運用ではClaude Codeがanalysis.jsonを見て経営者向け文章に書き換える。
    build.py --narrative-file 未指定時のフォールバック）。
    """
    blocks: list[str] = []

    if this_week_actions:
        action_lines = []
        for a in this_week_actions:
            # 「→」は「・」と縦位置を揃えるため全角スペース1つ分右にインデントする
            action_lines.append(
                f"・{a['action_target']}: {_action_observation(a)}\n　→{_action_evaluation(a)}\n　→{a['recommendation']}"
            )
        blocks.append("● 今週の評価\n\n" + "\n\n".join(action_lines))
    else:
        blocks.append("● 今週の評価\n断定的な施策変更を行うタイミングではなく、継続観測を優先する")

    presence_parts = [f"{row['market_label']}: {_market_presence_label(row)}" for row in market_table]
    blocks.append("● 市場存在感\n" + "／".join(presence_parts))

    cited = sum(1 for row in market_table if row["aio_status"] == "引用あり")
    comparison_missing = market_table and not any(row["competitor_comparison_available"] for row in market_table)
    c_line = f"● AIO\n重点{len(market_table)}市場すべてでAIOは生成されたが、TCD引用は{cited}件。"
    if comparison_missing:
        c_line += "今週は計測方法（対象期間の決定方法）を変更したため、競合比較は来週から再開する。"
    else:
        c_line += "競合ドメインの動向は④参照。"
    blocks.append(c_line)

    return blocks


def build_view_model(analysis: dict, thresholds_cfg: dict | None = None) -> dict:
    thresholds_cfg = thresholds_cfg or config_loader.load_thresholds()
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

    category_page_trends, other_pages = _build_category_page_trends(page_findings, thresholds_cfg, priority_market_urls)
    initiatives_section, unknown_initiatives = _build_initiatives_section(initiative_findings)

    site_usage = _build_site_usage(analysis.get("ga4_summary", {}))

    data_quality = analysis["data_quality"]
    data_summary = analysis.get("data_summary", {})

    header = {
        "period": analysis["period"],
        "comparison_available": data_summary.get("gsc", {}).get("comparison_available"),
        "data_quality_status": data_quality["status"],
        "weeks_available": analysis.get("weeks_available"),
        "low_impressions_threshold": thresholds_cfg["low_data"]["low_impressions"],
    }

    this_week_actions = _build_this_week_actions(all_sorted, thresholds_cfg, n=3)

    executive_summary = _build_executive_summary(priority_markets["table"], this_week_actions)

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
        "priority_markets": priority_markets,
        "category_page_trends": category_page_trends,
        "initiatives": initiatives_section,
        "this_week_actions": this_week_actions,
        "appendix": appendix,
    }
