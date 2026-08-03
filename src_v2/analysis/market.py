"""重点市場・競合・AIO分析。

config_v2/queries.yaml の markets（4市場）について、SEO状態・AIO状態・
AIO競合引用の変化をFindingとして生成する。

【2026-07-27 方針転換】完全一致（exact match）による集計は廃止した。
「東京 インナーブランディング会社」「インナーブランディング会社 東京」等の
語順違いをすべて別クエリとして扱うと実際の検索需要を取りこぼすため、
markets.*.all に列挙したトークンをすべて（順序不問で）含むクエリを対象とする
AND条件マッチング（normalize.matches_all_tokens）を唯一の正とする。
Looker Studio側の集計（RE2正規表現ベース）は比較参照専用とし、本モジュールの
出力を正本として扱う。

一致した実クエリの内訳は evidence.matched_query_variants に必ず保持し、
「集計結果がどの生クエリ由来か分からない」状態を作らない（根拠追跡性の担保）。
"""
from __future__ import annotations

from src_v2 import normalize
from src_v2.analysis.common import (
    expected_ctr,
    group_metrics,
    make_finding,
    position_delta,
    sum_metrics,
    top_n_by,
)
from src_v2.analysis.confidence import assess_confidence

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def _escalate(current: str, at_least: str) -> str:
    return current if _SEVERITY_ORDER[current] >= _SEVERITY_ORDER[at_least] else at_least


def _matched_rows(rows: list[dict], tokens: list[str]) -> list[dict]:
    return [r for r in rows if normalize.matches_all_tokens(r["query"], tokens)]


def _matched_query_variants(rows: list[dict]) -> list[dict]:
    """AND条件に一致した生クエリの内訳（監査用）。どのクエリが集計に
    寄与したかを常に追跡できるようにする。"""
    grouped = group_metrics(rows, key_fn=lambda r: r["query"])
    return [
        {"query": query, **metrics}
        for query, metrics in sorted(grouped.items(), key=lambda kv: -kv[1]["impressions"])
    ]


def _top_url_for_tokens(page_query_rows: list[dict], tokens: list[str]) -> dict | None:
    matched = _matched_rows(page_query_rows, tokens)
    if not matched:
        return None
    grouped = group_metrics(matched, key_fn=lambda r: r.get("page_id") or r["normalized_url"])
    ranked = top_n_by(grouped, n=1, sort_key="clicks")
    if not ranked:
        return None
    key, metrics = ranked[0]
    return {"page_id_or_url": key, **metrics}


def _judge_seo(current: dict, prv: dict, thresholds: dict) -> tuple[str, str, str, str]:
    """(severity, judgment, message, recommendation) を返す。"""
    low_impressions = thresholds["low_data"]["low_impressions"]
    low_clicks = thresholds["low_data"]["low_clicks"]
    meaningful = thresholds["low_data"]["position_change_meaningful"]

    if (current["impressions"] or 0) < low_impressions and (current["clicks"] or 0) < low_clicks:
        return ("info", "判断保留", "表示回数・クリック数が少なく参考値", "データ蓄積を待って再評価する")

    position = current["position"]
    if position is None:
        return ("warning", "再調査", "対象クエリでの表示が確認できない（Search Console上に該当データなし）",
                "検索結果に表示されているか、対象URLがインデックスされているかを確認する")

    delta = position_delta(position, prv.get("position"))

    if position <= 5:
        exp_ctr = expected_ctr(position)
        if exp_ctr is not None and current["ctr"] < exp_ctr * 0.7:
            return ("info", "再調査", f"上位表示（{position:.1f}位）だがCTRが期待値（目安{exp_ctr:.1%}）より低い",
                    "表示URLを確認／タイトル・descriptionを競合と比較")
        return ("info", "継続観測", f"上位表示を維持（{position:.1f}位）", "現状の掲載内容を継続観測する")

    if position > 10:
        return ("info", "再調査", f"10位以下（{position:.1f}位）",
                "ページ×クエリで表示URLを確認／検索結果のタイトル・descriptionを競合と比較／内部リンクを確認")

    if delta is not None and delta <= -meaningful:
        return ("warning", "再調査", f"順位が下落（{prv.get('position'):.1f}→{position:.1f}位）",
                "ページ×クエリで下落クエリを特定／検索結果のタイトル・descriptionを競合と比較／内部リンクを確認")
    if delta is not None and delta >= meaningful:
        return ("info", "継続観測", f"順位が改善（{prv.get('position'):.1f}→{position:.1f}位）", "改善傾向を継続観測する")

    return ("info", "継続観測", f"順位はおおむね横ばい（{position:.1f}位）", "継続観測する")


def analyze_market(
    bundle: dict,
    previous_bundle: dict | None,
    queries_cfg: dict,
    thresholds_cfg: dict,
    weeks_available: int = 1,
    recent_bundles: list[tuple[str, dict]] | None = None,
) -> list[dict]:
    """recent_bundles: (cur_end, bundle) を古い順に並べた直近最大4週分（当週含む）。
    AIO競合ドメインの複数週継続性判定に使う。省略時は当週のみで判定する
    （1回だけの新規引用は継続性なしとして扱われる＝安全側に倒れる）。
    """
    findings: list[dict] = []

    query_daily_cur = bundle["gsc"]["current"]["gsc_query_daily"]
    query_daily_prv = bundle["gsc"]["comparison"]["gsc_query_daily"]
    page_query_cur = bundle["gsc"]["current"]["gsc_page_query_daily"]
    aio_by_query = {o["query"]: o for o in bundle.get("aio_observations", [])}
    prev_aio_by_query = {o["query"]: o for o in (previous_bundle or {}).get("aio_observations", [])}

    domain_history = _build_domain_history(recent_bundles or [(bundle["period"]["cur_end"], bundle)])
    cross_market_counts = _cross_market_domain_counts(bundle)
    cur_end = bundle["period"]["cur_end"]

    for query_id, m in queries_cfg["markets"].items():
        representative_query = m["query"]
        tokens = m["all"]

        matched_cur = _matched_rows(query_daily_cur, tokens)
        matched_prv = _matched_rows(query_daily_prv, tokens)
        current = sum_metrics(matched_cur)
        comparison = sum_metrics(matched_prv)
        matched_query_variants = _matched_query_variants(matched_cur)

        top_url = _top_url_for_tokens(page_query_cur, tokens)
        aio_obs = aio_by_query.get(representative_query)

        severity, judgment, message, recommendation = _judge_seo(current, comparison, thresholds_cfg)

        aio_note = "AIO観測データなし"
        seo_aio_agree = None
        if aio_obs is not None:
            status = aio_obs["acquisition_status"]
            if status == "success":
                if aio_obs["aio_generated"] and not aio_obs["tcd_cited"]:
                    aio_note = "AIO検索結果に、TCDは引用されていない"
                    severity = _escalate(severity, "warning")
                    seo_direction = "positive" if (position_delta(current["position"], comparison.get("position")) or 0) > 0 else "negative"
                    seo_aio_agree = seo_direction == "negative"
                elif aio_obs["aio_generated"] and aio_obs["tcd_cited"]:
                    aio_note = "AIOで生成されTCDが引用されている"
                    seo_direction = "positive" if (position_delta(current["position"], comparison.get("position")) or 0) >= 0 else "negative"
                    seo_aio_agree = seo_direction == "positive"
                else:
                    aio_note = "AIOは生成されていない（observed）"
            elif status == "not_generated":
                aio_note = "AIOは生成されていない（observed）"
            else:
                aio_note = f"AIO観測が未完了（{status}）"

        competitor_summary = _competitor_summary(aio_obs, prev_aio_by_query.get(representative_query))

        evidence = {
            "query": representative_query, "market": m["market"], "region": m["region"],
            "match_tokens": tokens,
            "current": current, "comparison": comparison,
            "matched_query_variants": matched_query_variants,
            "top_url": top_url,
            "aio": aio_obs,
            "competitor_summary": competitor_summary,
        }

        confidence = assess_confidence({
            "impressions": current["impressions"],
            "clicks": current["clicks"],
            "weeks_available": weeks_available,
            "seo_aio_agree": seo_aio_agree,
            "page_query_agree": None,
        }, thresholds_cfg)

        findings.append(make_finding(
            id=f"market-{query_id}",
            category="market",
            severity=severity,
            confidence=confidence["level"],
            message=f"{message}／{aio_note}",
            recommendation=recommendation,
            judgment=judgment,
            evidence=evidence,
            source={"query_id": query_id, "query": representative_query},
        ))

        findings.extend(_competitor_findings(
            query_id, representative_query, competitor_summary,
            domain_history.get(representative_query, {}), cross_market_counts, cur_end,
        ))

    return findings


def _build_domain_history(recent_bundles: list[tuple[str, dict]]) -> dict[str, dict[str, set[str]]]:
    """query -> domain -> その domain が引用された週(cur_end)の集合。直近最大4週分（当週含む）。"""
    history: dict[str, dict[str, set[str]]] = {}
    for cur_end, b in recent_bundles:
        for obs in b.get("aio_observations", []):
            if obs.get("acquisition_status") != "success":
                continue
            query = obs.get("query")
            for d in (obs.get("cited_domains") or []):
                if not d or d == "tcd.jp":
                    continue
                history.setdefault(query, {}).setdefault(d, set()).add(cur_end)
    return history


def _cross_market_domain_counts(bundle: dict) -> dict[str, int]:
    """当週、同一ドメインが何件の重点市場クエリで引用されているか。"""
    counts: dict[str, int] = {}
    for obs in bundle.get("aio_observations", []):
        if obs.get("acquisition_status") != "success":
            continue
        for d in set(obs.get("cited_domains") or []):
            if not d or d == "tcd.jp":
                continue
            counts[d] = counts.get(d, 0) + 1
    return counts


def _competitor_summary(current_obs: dict | None, previous_obs: dict | None) -> dict:
    """AIO引用競合ドメインの状況（件数・前週比・新規/消失）。
    比較不可（初回観測・前回未成功等）の場合も comparison_available=False で
    明示する（「変化なし」と「未取得のため比較不能」を混同しない）。
    """
    current_available = bool(current_obs and current_obs.get("acquisition_status") == "success")
    current_domains = sorted(
        d for d in ((current_obs.get("cited_domains") or []) if current_available else []) if d and d != "tcd.jp"
    )

    comparison_available = bool(
        current_available and previous_obs and previous_obs.get("acquisition_status") == "success"
    )

    if not comparison_available:
        return {
            "current_count": len(current_domains) if current_available else None,
            "current_domains": current_domains,
            "previous_count": None,
            "new_domains": [],
            "dropped_domains": [],
            "comparison_available": False,
        }

    previous_domains = {d for d in (previous_obs.get("cited_domains") or []) if d and d != "tcd.jp"}
    current_set = set(current_domains)

    return {
        "current_count": len(current_domains),
        "current_domains": current_domains,
        "previous_count": len(previous_domains),
        "new_domains": sorted(current_set - previous_domains),
        "dropped_domains": sorted(previous_domains - current_set),
        "comparison_available": True,
    }


def _competitor_findings(
    query_id: str,
    priority_query: str,
    summary: dict,
    domain_periods: dict[str, set[str]],
    cross_market_counts: dict[str, int],
    cur_end: str,
) -> list[dict]:
    """新規ドメインは、以下のいずれかを満たす場合のみ severity=warning（Priority Action対象）とする。
    1回だけの新規引用は severity=info・judgment=継続観測 に倒す（単週変化を参入と断定しない）。
      - 直近4回の観測中2回以上（当週含む）引用 ＝ 実質「2週連続」も含む
      - 当週、複数の重点市場で引用
    """
    if not summary.get("comparison_available"):
        return []

    new_domains = summary["new_domains"]
    dropped_domains = summary["dropped_domains"]

    findings: list[dict] = []
    for domain in new_domains:
        periods_cited = domain_periods.get(domain, {cur_end})
        market_count = cross_market_counts.get(domain, 0)
        is_recurring = len(periods_cited) >= 2
        is_multi_market = market_count >= 2

        if is_recurring or is_multi_market:
            reasons = []
            if is_recurring:
                reasons.append(f"直近{len(periods_cited)}回の観測中{len(periods_cited)}回引用")
            if is_multi_market:
                reasons.append(f"{market_count}市場で引用")
            findings.append(make_finding(
                id=f"competitor-{query_id}-new-{domain}",
                category="competitor",
                severity="warning",
                confidence="Medium",
                message=f"「{priority_query}」のAIO引用に競合ドメイン「{domain}」が継続的に確認された（{'・'.join(reasons)}）",
                recommendation="新規引用ドメインの実績・コンテンツを確認し、差別化ポイントを検討する",
                judgment="再調査",
                evidence={
                    "query": priority_query, "domain": domain, "direction": "new",
                    "periods_cited": sorted(periods_cited), "cross_market_count": market_count,
                },
                source={"query_id": query_id, "query": priority_query},
            ))
        else:
            findings.append(make_finding(
                id=f"competitor-{query_id}-new-{domain}",
                category="competitor",
                severity="info",
                confidence="Low",
                message=f"「{priority_query}」のAIO引用に新規ドメイン「{domain}」が確認された（今回が初回、継続性は未確認）",
                recommendation="継続観測する（1回のみの新規引用では参入と断定しない）",
                judgment="継続観測",
                evidence={
                    "query": priority_query, "domain": domain, "direction": "new",
                    "periods_cited": sorted(periods_cited), "cross_market_count": market_count,
                },
                source={"query_id": query_id, "query": priority_query},
            ))

    if dropped_domains:
        findings.append(make_finding(
            id=f"competitor-{query_id}-dropped",
            category="competitor",
            severity="info",
            confidence="Medium",
            message=f"「{priority_query}」のAIO引用から前回確認されたドメインが消失: {', '.join(dropped_domains)}",
            recommendation="継続観測する（消失は一時的な変動の可能性もあるため断定しない）",
            judgment="継続観測",
            evidence={"query": priority_query, "domains": dropped_domains, "direction": "dropped"},
            source={"query_id": query_id, "query": priority_query},
        ))

    return findings
